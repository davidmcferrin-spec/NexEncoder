"""Read the ffmpeg progress file a running channel leaves behind."""

from __future__ import annotations

import time
from pathlib import Path


def parse_ffmpeg_progress(text: str) -> dict[str, str]:
    """Return the last completed progress block. ffmpeg appends blocks to the file."""
    block: dict[str, str] = {}
    last: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key == "progress":
            if block:
                last = block
            block = {}
            continue
        block[key] = value
    return last or block


def progress_bitrate_bps(fields: dict[str, str]) -> float | None:
    raw = fields.get("bitrate") or ""
    suffix = "kbits/s"
    if not raw.endswith(suffix) or raw == "N/A":
        return None
    try:
        return float(raw[: -len(suffix)]) * 1000
    except ValueError:
        return None


def signals_from_progress(state_dir: Path, now: float | None = None, fresh_seconds: float = 8.0) -> dict:
    """A channel is up only while its progress file is recent and frames are flowing."""
    clock = time.time() if now is None else now
    signals: dict[str, dict] = {}
    if not state_dir.is_dir():
        return signals
    for channel_id in ("A", "B"):
        path = state_dir / f"{channel_id}.progress"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[-16000:]
            mtime = path.stat().st_mtime
        except OSError:
            continue
        fields = parse_ffmpeg_progress(text)
        try:
            frame = int(float(fields.get("frame") or "0"))
        except ValueError:
            frame = 0
        try:
            dropped = int(float(fields.get("drop_frames") or "0"))
        except ValueError:
            dropped = 0
        fresh = (clock - mtime) <= fresh_seconds and frame > 0
        signals[channel_id] = {
            "locked": fresh,
            "audio": fresh,
            "running": fresh,
            "bitrate_bps": progress_bitrate_bps(fields) if fresh else None,
            "dropped": dropped,
        }
    return signals


def progress_frame(path: Path | None) -> int:
    """Latest frame count from a progress file. Missing or unreadable files are 0."""
    if path is None or not path.is_file():
        return 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[-8000:]
    except OSError:
        return 0
    try:
        return int(float(parse_ffmpeg_progress(text).get("frame") or "0"))
    except ValueError:
        return 0
