"""Two-channel configuration and the metrics snapshot it produces."""

from __future__ import annotations

import json
from pathlib import Path

from .metrics import InputStatus, OutputStatus, Snapshot
from .signal import signal_mode

CHANNELS = ("A", "B")
FORMATS = frozenset({"1080i59.94", "1080i50", "720p59.94", "720p50"})
MODES = frozenset({"srt-caller", "srt-listener", "hls-push", "hls-pull"})

DEFAULT_DEVICES = {
    "A": "DeckLink Duo 2 (1)",
    "B": "DeckLink Duo 2 (2)",
}


def default_config() -> dict:
    return {
        "inputs": {
            "A": {"format": "1080i59.94", "device": DEFAULT_DEVICES["A"]},
            "B": {"format": "1080i59.94", "device": DEFAULT_DEVICES["B"]},
        },
        "outputs": {
            "A": {"mode": "srt-listener", "bitrate": 8, "host": "", "port": 9000, "url": ""},
            "B": {"mode": "srt-listener", "bitrate": 8, "host": "", "port": 9001, "url": ""},
        },
    }


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
        device = src.get("device")
        if isinstance(device, str) and device.strip() and len(device) <= 128 and "\n" not in device:
            base["inputs"][channel_id]["device"] = device.strip()
        out = incoming_outputs.get(channel_id) if isinstance(incoming_outputs.get(channel_id), dict) else {}
        if out.get("mode") in MODES:
            base["outputs"][channel_id]["mode"] = out["mode"]
        bitrate = out.get("bitrate")
        if isinstance(bitrate, int) and not isinstance(bitrate, bool) and 6 <= bitrate <= 18:
            base["outputs"][channel_id]["bitrate"] = bitrate
        host = out.get("host")
        if isinstance(host, str) and len(host) <= 253 and " " not in host and "\n" not in host:
            base["outputs"][channel_id]["host"] = host
        port = out.get("port")
        if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
            base["outputs"][channel_id]["port"] = port
        url = out.get("url")
        if isinstance(url, str) and len(url) <= 500 and (url == "" or url.startswith(("http://", "https://"))):
            base["outputs"][channel_id]["url"] = url
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
        delivery = config["outputs"][channel_id]
        running = bool(live.get("running"))
        outputs.append(OutputStatus(
            channel_id,
            up=running,
            connected=bool(live.get("connected")),
            mode=delivery["mode"],
            video_bitrate_bps=live.get("bitrate_bps") if running else None,
            video_bitrate_target_bps=delivery["bitrate"] * 1_000_000,
            width=mode.width if running else None,
            height=mode.height if running else None,
            dropped_frames=int(live.get("dropped") or 0),
            rtt_seconds=live.get("rtt_seconds"),
            packets_lost=live.get("packets_lost"),
        ))
    return Snapshot(tuple(inputs), tuple(outputs))
