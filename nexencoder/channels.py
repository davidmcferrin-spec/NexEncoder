"""Two-channel configuration and the metrics snapshot it produces.

Each input encodes once. ``outputs`` is a list of one to four destinations
that read that encode. A saved config from before that list still loads:
a single object becomes destination ``1``, and its bitrate moves onto the input.
"""

from __future__ import annotations

import json
from pathlib import Path

from .metrics import InputStatus, OutputStatus, Snapshot
from .signal import signal_mode

CHANNELS = ("A", "B")
FORMATS = frozenset({"1080i59.94", "1080i50", "1080p59.94", "1080p60", "1080p50", "720p59.94", "720p50"})
MODES = frozenset({"srt-caller", "srt-listener", "hls-push", "hls-pull"})
SOURCES = frozenset({"sdi", "srt"})
WAITING = frozenset({"slate", "loop", "bars"})
# Input listeners are fixed. An output listener must not bind these ports.
INPUT_SRT_PORTS = {"A": 9001, "B": 9002}
RESERVED_LISTENER_PORTS = frozenset(INPUT_SRT_PORTS.values())
MAX_OUTPUTS = 4
SLOTS = ("1", "2", "3", "4")
# Extra listeners that collide start here. Slot 1 keeps 9000 and 9101.
LISTENER_SCAN_START = 9200

DEFAULT_DEVICES = {
    "A": "DeckLink Duo (1)",
    "B": "DeckLink Duo (2)",
}
DEFAULT_LISTENER_PORTS = {"A": 9000, "B": 9101}


def _destination(channel_id: str, slot: str = "1") -> dict:
    return {
        "id": slot,
        "mode": "srt-listener",
        "host": "",
        "port": DEFAULT_LISTENER_PORTS[channel_id],
        "url": "",
    }


def default_config() -> dict:
    return {
        "inputs": {
            "A": {
                "format": "1080i59.94",
                "device": DEFAULT_DEVICES["A"],
                "source": "sdi",
                "waiting": "slate",
                "loop": "",
                "bitrate": 8,
            },
            "B": {
                "format": "1080i59.94",
                "device": DEFAULT_DEVICES["B"],
                "source": "sdi",
                "waiting": "slate",
                "loop": "",
                "bitrate": 8,
            },
        },
        "outputs": {
            "A": [_destination("A")],
            "B": [_destination("B")],
        },
    }


def _bitrate(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 6 <= value <= 18:
        return value
    return None


def _as_destinations(raw: object) -> list[dict]:
    """A legacy object is one destination. A list is capped at four."""
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)][:MAX_OUTPUTS]
    return []


def _legacy_bitrate(items: list[dict]) -> int | None:
    for item in items:
        found = _bitrate(item.get("bitrate"))
        if found is not None:
            return found
    return None


def _clean_destination(item: dict, fallback_port: int) -> dict:
    dest = {"id": "", "mode": "srt-listener", "host": "", "port": fallback_port, "url": ""}
    slot = item.get("id")
    if isinstance(slot, bool):
        slot = None
    elif isinstance(slot, int) and str(slot) in SLOTS:
        slot = str(slot)
    if isinstance(slot, str) and slot in SLOTS:
        dest["id"] = slot
    if item.get("mode") in MODES:
        dest["mode"] = item["mode"]
    host = item.get("host")
    if isinstance(host, str) and len(host) <= 253 and " " not in host and "\n" not in host and "\r" not in host:
        dest["host"] = host
    port = item.get("port")
    if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
        if dest["mode"] == "srt-listener" and port in RESERVED_LISTENER_PORTS:
            port = 9101 if port == 9001 else 9102
        dest["port"] = port
    url = item.get("url")
    if isinstance(url, str) and len(url) <= 500 and (url == "" or url.startswith(("http://", "https://"))):
        dest["url"] = url
    return dest


def _assign_slots(items: list[dict]) -> None:
    seen: set[str] = set()
    for item in items:
        slot = item["id"]
        if slot not in SLOTS or slot in seen:
            item["id"] = ""
        else:
            seen.add(slot)
    free = [slot for slot in SLOTS if slot not in seen]
    for item in items:
        if item["id"] == "":
            item["id"] = free.pop(0)


def _next_listener_port(used: set[int]) -> int:
    port = LISTENER_SCAN_START
    while port <= 65535 and (port in used or port in RESERVED_LISTENER_PORTS):
        port += 1
    if port > 65535:
        return LISTENER_SCAN_START
    return port


def _unique_listeners(config: dict) -> None:
    """One listener port per destination, and never an input port."""
    used = set(RESERVED_LISTENER_PORTS)
    for channel_id in CHANNELS:
        for dest in config["outputs"][channel_id]:
            if dest["mode"] != "srt-listener":
                continue
            port = int(dest["port"])
            if port in used:
                port = _next_listener_port(used)
                dest["port"] = port
            used.add(port)


def hls_publish_dir(root: Path, channel_id: str, slot: str) -> Path:
    """Playlist directory. Slot 1 keeps the original ``hls/<channel>`` path."""
    base = root / channel_id
    if slot == "1":
        return base
    return base / slot


def srt_stats_path(state_dir: Path, channel_id: str, slot: str) -> Path:
    """Slot 1 keeps ``A.srt.json``. Later slots are ``A-2.srt.json``."""
    name = f"{channel_id}.srt.json" if slot == "1" else f"{channel_id}-{slot}.srt.json"
    return state_dir / name


def normalize_config(raw: dict) -> dict:
    """Keep only known channels and legal values. Anything else falls back."""
    base = default_config()
    incoming_inputs = raw.get("inputs") if isinstance(raw, dict) else None
    incoming_outputs = raw.get("outputs") if isinstance(raw, dict) else None
    if not isinstance(incoming_inputs, dict):
        incoming_inputs = {}
    if not isinstance(incoming_outputs, dict):
        incoming_outputs = {}
    for channel_id in CHANNELS:
        src = incoming_inputs.get(channel_id) if isinstance(incoming_inputs.get(channel_id), dict) else {}
        if src.get("format") in FORMATS:
            base["inputs"][channel_id]["format"] = src["format"]
        base["inputs"][channel_id]["device"] = DEFAULT_DEVICES[channel_id]
        if src.get("source") in SOURCES:
            base["inputs"][channel_id]["source"] = src["source"]
        if src.get("waiting") in WAITING:
            base["inputs"][channel_id]["waiting"] = src["waiting"]
        loop = src.get("loop")
        if isinstance(loop, str) and len(loop) <= 500 and "\n" not in loop and "\r" not in loop:
            base["inputs"][channel_id]["loop"] = loop.strip()
        items = _as_destinations(incoming_outputs.get(channel_id))
        bitrate = _bitrate(src.get("bitrate"))
        if bitrate is None:
            bitrate = _legacy_bitrate(items)
        if bitrate is not None:
            base["inputs"][channel_id]["bitrate"] = bitrate
        if not items:
            continue
        fallback = DEFAULT_LISTENER_PORTS[channel_id]
        cleaned = [_clean_destination(item, fallback) for item in items]
        _assign_slots(cleaned)
        base["outputs"][channel_id] = cleaned
    _unique_listeners(base)
    return base


def load_config(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_config()
    if not isinstance(raw, dict):
        return default_config()
    return normalize_config(raw)


def save_config(path: Path, raw: dict) -> dict:
    config = normalize_config(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return config


def _delivery_signal(live: dict, slot: str) -> tuple[bool, object, object]:
    """Connection for one destination. Slot 1 still reads the channel-level fields."""
    per = live.get("destinations") if isinstance(live.get("destinations"), dict) else {}
    detail = per.get(slot) if isinstance(per.get(slot), dict) else None
    if detail is None and slot == "1":
        return bool(live.get("connected")), live.get("rtt_seconds"), live.get("packets_lost")
    if detail is None:
        return False, None, None
    return bool(detail.get("connected")), detail.get("rtt_seconds"), detail.get("packets_lost")


def snapshot_for(config: dict, signals: dict | None = None) -> Snapshot:
    """Build the Prometheus snapshot. Signal fields appear only when a feed is locked."""
    signals = signals or {}
    inputs = []
    outputs = []
    for channel_id in CHANNELS:
        spec = config["inputs"][channel_id]
        mode = signal_mode(spec["format"])
        live = signals.get(channel_id) if isinstance(signals.get(channel_id), dict) else {}
        locked = bool(live.get("locked"))
        inputs.append(InputStatus(
            channel_id,
            up=locked,
            format_name=spec["format"],
            scan="interlaced" if mode.interlaced else "progressive",
            audio="locked" if live.get("audio") else "unlocked",
            width=mode.width if locked else None,
            height=mode.height if locked else None,
            field_rate_hz=round(mode.field_rate_hz, 2) if locked else None,
            captions_up=bool(live.get("captions")),
            scte104_messages=int(live.get("scte104") or 0),
        ))
        running = bool(live.get("running"))
        target = int(spec["bitrate"]) * 1_000_000
        for dest in config["outputs"][channel_id]:
            connected, rtt, lost = _delivery_signal(live, dest["id"])
            outputs.append(OutputStatus(
                channel_id,
                up=running,
                connected=connected,
                mode=dest["mode"],
                video_bitrate_bps=live.get("bitrate_bps") if running else None,
                video_bitrate_target_bps=target,
                width=mode.width if running else None,
                height=mode.height if running else None,
                dropped_frames=int(live.get("dropped") or 0),
                rtt_seconds=rtt,
                packets_lost=lost,
                slot=dest["id"],
            ))
    return Snapshot(tuple(inputs), tuple(outputs))
