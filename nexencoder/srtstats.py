"""TSDuck SRT statistics: probe, parse, and mirror the latest report to disk.

The program egress for SRT is ``tsp -O srt``, not FFmpeg and not GStreamer.
TSDuck prints one JSON object per interval when ``--json-line`` is set. Field
names vary by release, so the parser accepts the hierarchical report and the
older flat libsrt names.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("nexencoder.srt")

_LOG_SECRET = re.compile(r"(?i)((?:passphrase|password|token|secret)=)[^&\s]+")
_LOG_USERINFO = re.compile(r"(://[^/\s:@]+:)[^@\s/]+@")

_PROBE: bool | None = None
_RTT_KEYS = {"rtt-ms", "ms-rtt", "msrtt", "rtt_ms", "rtt"}
_LOST_KEYS = {"lost", "pkt-sent-loss", "pktsndloss", "pktsndlosstotal", "packets-lost"}
_REXMIT_KEYS = {"retransmit", "retransmits", "pkt-retransmitted", "pktretrans", "pktretranstotal"}
_DROP_KEYS = {"dropped", "pkt-send-drop", "pktsnddrop", "pktsnddroptotal", "packets-dropped"}
_MBPS_KEYS = {"rate-mbps", "mbps-send-rate", "mbpssendrate", "bandwidth-mbps"}
NORMAL_KEYS = (
    "rtt_ms",
    "packets_lost",
    "packets_lost_interval",
    "retransmits",
    "retransmits_interval",
    "packets_dropped",
    "bandwidth_bps",
)


def tsp_supports_srt_stats(tsp: str = "tsp", runner=None) -> bool:
    """True when this tsp binary documents both statistics options."""
    global _PROBE
    if runner is None and tsp == "tsp" and _PROBE is not None:
        return _PROBE
    if runner is None and shutil.which(tsp) is None:
        if tsp == "tsp":
            _PROBE = False
        return False

    def default_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False)

    try:
        proc = (runner or default_run)([tsp, "-O", "srt", "--help"])
    except (OSError, subprocess.TimeoutExpired):
        result = False
    else:
        text = f"{getattr(proc, 'stdout', '') or ''}\n{getattr(proc, 'stderr', '') or ''}"
        result = "--statistics-interval" in text and "--json-line" in text
    if runner is None and tsp == "tsp":
        _PROBE = result
    return result


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_int(value: object) -> int | None:
    number = _as_float(value)
    if number is None:
        return None
    return int(number)


def _find(node: object, names: set[str], path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], str, float]]:
    found: list[tuple[tuple[str, ...], str, float]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).lower()
            child = path + (lowered,)
            number = _as_float(value)
            if lowered in names and number is not None:
                found.append((child, lowered, number))
            found.extend(_find(value, names, child))
    elif isinstance(node, list):
        for item in node:
            found.extend(_find(item, names, path))
    return found


def _pick(node: dict, names: set[str], needle: str) -> float | None:
    for path, _key, value in _find(node, names):
        if needle in path:
            return value
    return None


def _pick_any(node: dict, names: set[str]) -> float | None:
    found = _find(node, names)
    for path, key, value in found:
        if key.endswith("total") or "total" in path:
            return value
    if not found:
        return None
    return found[0][2]


def _blank() -> dict[str, float | int | None]:
    return {key: None for key in NORMAL_KEYS}


def normalize_srt_dict(node: dict) -> dict[str, float | int | None] | None:
    """Flatten one TSDuck or libsrt stats object. Already-flat objects pass through."""
    if any(key in node for key in NORMAL_KEYS):
        stats = _blank()
        for key in NORMAL_KEYS:
            if key == "rtt_ms":
                stats[key] = _as_float(node.get(key))
            elif key == "bandwidth_bps":
                number = _as_float(node.get(key))
                stats[key] = None if number is None else int(round(number))
            else:
                stats[key] = _as_int(node.get(key))
        if all(value is None for value in stats.values()):
            return None
        return stats

    stats = _blank()
    rtt = _pick_any(node, _RTT_KEYS)
    if rtt is not None:
        stats["rtt_ms"] = rtt
    lost_total = _pick(node, _LOST_KEYS, "total")
    lost_interval = _pick(node, _LOST_KEYS, "interval")
    if lost_total is None and lost_interval is None:
        lost_total = _pick_any(node, _LOST_KEYS)
    rex_total = _pick(node, _REXMIT_KEYS, "total")
    rex_interval = _pick(node, _REXMIT_KEYS, "interval")
    if rex_total is None and rex_interval is None:
        rex_total = _pick_any(node, _REXMIT_KEYS)
    dropped = _pick(node, _DROP_KEYS, "total")
    if dropped is None:
        dropped = _pick_any(node, _DROP_KEYS)
    rate = None
    for path, _key, value in _find(node, _MBPS_KEYS):
        if rate is None or "send" in path:
            rate = value
            if "send" in path:
                break
    stats["packets_lost"] = _as_int(lost_total)
    stats["packets_lost_interval"] = _as_int(lost_interval)
    stats["retransmits"] = _as_int(rex_total)
    stats["retransmits_interval"] = _as_int(rex_interval)
    stats["packets_dropped"] = _as_int(dropped)
    if rate is not None:
        stats["bandwidth_bps"] = int(round(rate * 1_000_000))
    if all(value is None for value in stats.values()):
        return None
    return stats


def _json_objects(text: str) -> list[dict]:
    decoder = json.JSONDecoder()
    found: list[dict] = []
    index = 0
    while True:
        start = text.find("{", index)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(obj, dict):
            found.append(obj)
        index = end if end > start else start + 1
    return found


def _ints_in(text: str) -> int:
    return int(text.replace(",", ""))


def normalize_srt_text(text: str) -> dict[str, float | int | None] | None:
    """Parse the human TSDuck statistics block. It has no send rate."""
    stats = _blank()
    rtt = re.search(r"RTT:\s*([0-9]+(?:\.[0-9]+)?)\s*ms", text, re.I)
    if rtt:
        stats["rtt_ms"] = float(rtt.group(1))
    total = re.search(
        r"Total sent:\s*([0-9,]+)\s*bytes,\s*([0-9,]+)\s*packets,\s*"
        r"retransmit:\s*([0-9,]+)\s*packets,\s*lost:\s*([0-9,]+)\s*packets"
        r"(?:,\s*dropped:\s*([0-9,]+)\s*packets)?",
        text,
        re.I,
    )
    if total:
        stats["retransmits"] = _ints_in(total.group(3))
        stats["packets_lost"] = _ints_in(total.group(4))
        if total.group(5):
            stats["packets_dropped"] = _ints_in(total.group(5))
    interval = re.search(
        r"Interval sent:\s*([0-9,]+)\s*bytes,\s*([0-9,]+)\s*packets,\s*"
        r"retransmit:\s*([0-9,]+)\s*packets,\s*lost:\s*([0-9,]+)\s*packets",
        text,
        re.I,
    )
    if interval:
        stats["retransmits_interval"] = _ints_in(interval.group(3))
        stats["packets_lost_interval"] = _ints_in(interval.group(4))
    if all(value is None for value in stats.values()):
        return None
    return stats


def parse_srt_report(text: str) -> dict[str, float | int | None] | None:
    """Normalize the last stats object in a log line or block."""
    last = None
    for obj in _json_objects(text):
        report = obj.get("report") if isinstance(obj.get("report"), dict) else obj
        if isinstance(report, dict):
            normalized = normalize_srt_dict(report)
            if normalized is not None:
                last = normalized
    if last is not None:
        return last
    return normalize_srt_text(text)


def redact_secrets(text: str) -> str:
    """Strip URL userinfo passwords and query secrets from a log line or argv."""
    cleaned = _LOG_USERINFO.sub(r"\1REDACTED@", text)
    return _LOG_SECRET.sub(r"\1REDACTED", cleaned)


def redact_log_line(line: str) -> str:
    """Strip SRT URL secrets before a tsp stderr line is written to the journal."""
    return redact_secrets(line)[:500]


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o644)
    except OSError:
        pass


def load_srt_snapshot(path: Path, now: float, fresh_seconds: float = 30.0) -> dict | None:
    """Read the mirrored stats file. Numbers older than ``fresh_seconds`` are cleared."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        mtime = path.stat().st_mtime
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(raw, dict):
        return None
    observed = raw.get("observed_unix")
    if not isinstance(observed, (int, float)) or isinstance(observed, bool):
        observed = mtime
    age = max(0.0, now - float(observed))
    stats = parse_srt_report(json.dumps(raw))
    if stats is None:
        return None
    fresh = age <= fresh_seconds
    body = {
        "available": fresh,
        "fresh": fresh,
        "source": "tsduck" if fresh else "stale",
        "age_seconds": round(age, 3),
    }
    for key in NORMAL_KEYS:
        body[key] = stats.get(key) if fresh else None
    return body


def empty_srt(source: str = "none") -> dict:
    body = {
        "available": False,
        "fresh": False,
        "source": source,
        "age_seconds": None,
    }
    for key in NORMAL_KEYS:
        body[key] = None
    return body


def start_srt_stats_reader(proc, dest: Path) -> None:
    """Copy the latest stats line from a tsp stderr pipe onto ``dest``.

    The reader is a no-op when the process has no stderr pipe (tests, or an
    egress started without statistics). Other stderr lines are logged so a
    tsp fault is still visible on the channel unit.
    """
    stream = getattr(proc, "stderr", None)
    if stream is None:
        return

    def run() -> None:
        try:
            while True:
                raw = stream.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                stats = parse_srt_report(line)
                if stats is None:
                    log.info("srt %s", redact_log_line(line))
                    continue
                payload = {"observed_unix": time.time(), **stats}
                try:
                    atomic_json(dest, payload)
                except OSError:
                    log.warning("could not write %s", dest)
        except Exception:
            log.warning("srt stats reader stopped", exc_info=True)

    threading.Thread(target=run, name="nexencoder-srt-stats", daemon=True).start()
