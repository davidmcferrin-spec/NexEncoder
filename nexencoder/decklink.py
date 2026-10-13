"""DeckLink device lists and status JSON. Parsing does not need the card."""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

_DEVICE_LINE = re.compile(r"""['"]([^'"]+)['"]""")


def parse_list_devices(text: str) -> list[dict[str, Any]]:
    """Parse `ffmpeg -f decklink -list_devices 1` stderr. Index follows list order."""
    devices: list[dict[str, Any]] = []
    header = False
    for line in text.splitlines():
        if "DeckLink" in line and "devices" in line.lower():
            header = True
            continue
        if not header:
            continue
        if "Immediate exit" in line or line.strip().startswith("dummy:"):
            break
        match = _DEVICE_LINE.search(line)
        if not match:
            continue
        name = match.group(1).strip()
        if not name or name.lower() == "dummy":
            continue
        devices.append({"index": len(devices), "name": name})
    return devices


def ffmpeg_has_decklink(text: str) -> bool:
    low = text.lower()
    if "unknown input format" in low and "decklink" in low:
        return False
    return "decklink" in low


def list_devices(ffmpeg: str = "ffmpeg", timeout: float = 8.0) -> list[dict[str, Any]]:
    """Ask ffmpeg for DeckLink inputs. An install without the card returns []."""
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-f", "decklink", "-list_devices", "1", "-i", "dummy"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    blob = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if not ffmpeg_has_decklink(blob):
        return []
    return parse_list_devices(blob)


def parse_status(text: str, device: str) -> dict[str, Any]:
    """Map a NexVUE-shaped status dump onto one sub-device.

    Busy inputs still report input_locked and input_mode.
    """
    unknown = {"signal": "unknown", "format": "", "detail": "DeckLink status is unavailable"}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return unknown
    devices = data.get("devices") if isinstance(data, dict) else None
    if not isinstance(devices, list):
        return unknown
    match = None
    for row in devices:
        if isinstance(row, dict) and str(row.get("name") or "") == device:
            match = row
            break
    if match is None:
        return {"signal": "unknown", "format": "", "detail": "status helper did not report this sub-device"}
    locked = bool(match.get("input_locked"))
    mode = str(match.get("input_mode") or "")
    if mode.lower() == "unknown":
        mode = ""
    return {
        "signal": "present" if locked else "no_signal",
        "format": mode if locked else "",
        "detail": "sub-device busy" if match.get("busy") else "",
    }
