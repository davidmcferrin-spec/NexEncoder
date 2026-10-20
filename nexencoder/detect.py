"""Find the raster on an SDI input or an SRT caller.

SDI is a short DeckLink open with format detection. That open needs the
sub-device free, so a running channel has to be stopped first. An SRT caller
is already inside the ingest process; Auto reads the stream banner that
process saved and does not bind the listener again.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path

from .channels import INPUT_SRT_PORTS, load_config, save_config
from .decklink import STATUS_HELPER, parse_status
from .effects import read_flag, signal_path, write_text_atomic

log = logging.getLogger("nexencoder.detect")

_LABELS = {
    "1080i59.94": "1080i 59.94",
    "1080i50": "1080i 50",
    "1080p29.97": "1080p 29.97",
    "1080p59.94": "1080p 59.94",
    "1080p60": "1080p 60",
    "1080p50": "1080p 50",
    "720p59.94": "720p 59.94",
    "720p50": "720p 50",
}

_DECKLINK_MODE = re.compile(
    r"Found Decklink mode\s+(\d+)\s*x\s*(\d+)\s+with rate\s+([0-9]+(?:\.[0-9]+)?)\s*(\(i\))?",
    re.IGNORECASE,
)
_SIZE = re.compile(r"(\d{3,5})x(\d{3,5})")
_FPS = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s+fps")


def source_path(state_dir: Path, channel_id: str) -> Path:
    return state_dir / f"{channel_id}.source.json"


def raster_from_geometry(width: int, height: int, fps: float, interlaced: bool) -> str | None:
    """Map a measured picture onto a raster this encoder can capture. Others are refused."""
    if width == 1920 and height == 1080 and interlaced:
        if _near(fps, 29.97) or _near(fps, 30) or _near(fps, 59.94) or _near(fps, 60):
            return "1080i59.94"
        if _near(fps, 25) or _near(fps, 50):
            return "1080i50"
        return None
    if width == 1920 and height == 1080 and not interlaced:
        if _integer_rate(fps, 60):
            return "1080p60"
        if _near(fps, 59.94):
            return "1080p59.94"
        if _near(fps, 50):
            return "1080p50"
        if _integer_rate(fps, 30):
            return None
        if _near(fps, 29.97):
            return "1080p29.97"
        return None
    if width == 1280 and height == 720 and not interlaced:
        if _near(fps, 59.94) or _near(fps, 60):
            return "720p59.94"
        if _near(fps, 50):
            return "720p50"
    return None


def parse_probe(text: str, target: str) -> dict:
    """Turn an ffmpeg banner or a DeckLink status dump into an Auto result."""
    status = _from_status_json(text, target)
    if status is not None:
        return status
    low = text.lower()
    if "unknown input format" in low and "decklink" in low:
        return _miss(f"This ffmpeg has no DeckLink input.")
    if _busy(low) and _DECKLINK_MODE.search(text) is None and "video:" not in low:
        return _miss(f"{target} is in use by the channel. Stop the channel, then press Auto.")
    found = _geometry(text)
    if found is None:
        if "no input signal" in low or "timed out" in low or "timeout" in low or not text.strip():
            return _miss(f"No signal on {target}.")
        if "address already in use" in low:
            return _miss(f"No SRT caller on {target}.")
        return _miss(f"No signal on {target}.")
    name = raster_from_geometry(found["width"], found["height"], found["fps"], found["interlaced"])
    if name is None:
        return _miss(f"{_describe(found)} is not a supported raster.")
    return {
        "ok": True,
        "format": name,
        "detail": f"{_LABELS[name]}. Restart the channel service to apply it.",
    }


def sdi_probe_argv(device: str, ffmpeg: str = "ffmpeg") -> list[str]:
    """One frame, no format_code, so DeckLink reports the locked mode."""
    return [
        ffmpeg, "-hide_banner", "-loglevel", "info",
        "-f", "decklink",
        "-raw_format", "uyvy422",
        "-i", device,
        "-frames:v", "1",
        "-f", "null", "-",
    ]


def srt_probe_argv(port: int, ffmpeg: str = "ffmpeg") -> list[str]:
    """Listen until one frame arrives. Used only when the channel is not already listening."""
    return [
        ffmpeg, "-hide_banner", "-loglevel", "info",
        "-i", f"srt://0.0.0.0:{int(port)}?mode=listener&transtype=live&latency=1500",
        "-frames:v", "1",
        "-f", "null", "-",
    ]


def run_probe(argv: list[str], timeout: float = 8.0) -> str:
    """Run ffmpeg and return its combined output, including a timeout's partial log."""
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _blob(exc.stdout) + "\n" + _blob(exc.stderr)
    except OSError as exc:
        return str(exc)
    return (done.stdout or "") + "\n" + (done.stderr or "")


def note_source_line(path: Path, line: str) -> None:
    """Save a supported or unsupported video banner from the SRT ingest log."""
    if "Video:" not in line:
        return
    found = _geometry(line)
    if found is None:
        return
    found["format"] = raster_from_geometry(found["width"], found["height"], found["fps"], found["interlaced"]) or ""
    write_text_atomic(path, json.dumps(found) + "\n")


def detect_channel(channel_id: str, config_path: Path, state_dir: Path, probe=None) -> dict:
    """Probe one input and, on a match, store that raster in the channel config."""
    if channel_id not in ("A", "B"):
        raise ValueError("channel must be A or B")
    config = load_config(config_path)
    spec = config["inputs"][channel_id]
    runner = probe or run_probe
    if spec.get("source") == "srt":
        found = _detect_srt(channel_id, state_dir, runner)
    elif probe is None and os.access(STATUS_HELPER, os.X_OK):
        found = parse_probe(run_probe([STATUS_HELPER], timeout=15), spec["device"])
    else:
        found = parse_probe(runner(sdi_probe_argv(spec["device"])), spec["device"])
    if not found["ok"]:
        return found
    config["inputs"][channel_id]["format"] = found["format"]
    found["config"] = save_config(config_path, config)
    return found


def _detect_srt(channel_id: str, state_dir: Path, probe) -> dict:
    port = INPUT_SRT_PORTS[channel_id]
    target = f"port {port}"
    if read_flag(signal_path(state_dir, channel_id), False):
        noted = _read_source(source_path(state_dir, channel_id))
        if noted is None:
            return _miss("An SRT caller is connected, but its format has not been read yet.")
        return _from_note(noted)
    text = probe(srt_probe_argv(port))
    return parse_probe(text, target)


def _from_note(noted: dict) -> dict:
    name = noted.get("format") or ""
    if name in _LABELS:
        return {
            "ok": True,
            "format": name,
            "detail": f"{_LABELS[name]}. Restart the channel service to apply it.",
        }
    if {"width", "height", "fps", "interlaced"} <= set(noted):
        return _miss(f"{_describe(noted)} is not a supported raster.")
    return _miss("An SRT caller is connected, but its format has not been read yet.")


def _read_source(path: Path) -> dict | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _from_status_json(text: str, target: str) -> dict | None:
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    status = parse_status(stripped, target)
    if status["signal"] == "unknown" and status["format"] == "" and "unavailable" in status["detail"]:
        return None
    if status["signal"] == "no_signal":
        return _miss(f"No signal on {target}.")
    if status["signal"] != "present" or not status["format"]:
        return None
    if status["format"] in _LABELS:
        return {
            "ok": True,
            "format": status["format"],
            "detail": f"{_LABELS[status['format']]}. Restart the channel service to apply it.",
        }
    return _miss(f"{status['format']} is not a supported raster.")


def _geometry(text: str) -> dict | None:
    match = None
    for match in _DECKLINK_MODE.finditer(text):
        pass
    if match is not None:
        width, height = int(match.group(1)), int(match.group(2))
        fps = float(match.group(3))
        return {
            "width": width,
            "height": height,
            "fps": fps,
            "interlaced": bool(match.group(4)),
        }
    found = None
    for line in text.splitlines():
        if "Video:" not in line:
            continue
        size = _SIZE.search(line)
        rate = _FPS.search(line)
        if size is None or rate is None:
            continue
        width, height = int(size.group(1)), int(size.group(2))
        fps = float(rate.group(1))
        found = {"width": width, "height": height, "fps": fps, "interlaced": _scan(line, width, height, fps)}
    return found


def _scan(text: str, width: int, height: int, fps: float) -> bool:
    low = text.lower()
    if "progressive" in low and not _field_words(text):
        return False
    if _field_words(text):
        return True
    return width == 1920 and height == 1080 and fps < 40


def _field_words(text: str) -> bool:
    low = text.lower()
    return any(word in low for word in ("(i)", "interlaced", "top first", "bottom first", "top coded", "bottom coded"))


def _busy(low: str) -> bool:
    return "resource busy" in low or "device is busy" in low or ("busy" in low and "decklink" in low)


def _describe(found: dict) -> str:
    scan = "interlaced" if found.get("interlaced") else "progressive"
    fps = found.get("fps")
    rate = f"{fps:.2f}".rstrip("0").rstrip(".")
    return f"{found.get('width')}x{found.get('height')} {scan} at {rate} fps"


def _miss(detail: str) -> dict:
    return {"ok": False, "format": "", "detail": detail}


def _near(value: float, target: float) -> bool:
    return abs(value - target) <= 0.6


def _integer_rate(value: float, target: float) -> bool:
    """True 60 fps, not 59.94. The wide match treats those as the same rate."""
    return abs(value - target) <= 0.02


def _blob(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)
