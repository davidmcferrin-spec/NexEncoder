"""DeckLink device lists and status JSON. Parsing does not need the card."""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from typing import Any

_DEVICE_LINE = re.compile(r"""['"]([^'"]+)['"]""")
STATUS_HELPER = "/usr/local/bin/decklink-status"
STATUS_TIMEOUT = 15.0
STATUS_INTERVAL = 5.0


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


def query_status(helper: str = STATUS_HELPER, runner=None, timeout: float = STATUS_TIMEOUT) -> dict[str, Any]:
    """Run decklink-status and return its device list.

    The helper prints one JSON object. A missing binary or a driver that is
    not loaded comes back as ok false with an empty device list.
    """
    if runner is None:
        try:
            proc = subprocess.run(
                [helper],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "devices": [], "detail": str(exc)}
        text = proc.stdout or ""
        if not text.strip():
            text = proc.stderr or ""
    else:
        try:
            text = runner([helper])
        except OSError as exc:
            return {"ok": False, "devices": [], "detail": str(exc)}
    start = text.find("{")
    if start < 0:
        return {"ok": False, "devices": [], "detail": "decklink-status returned no JSON"}
    try:
        data = json.loads(text[start:])
    except json.JSONDecodeError:
        return {"ok": False, "devices": [], "detail": "decklink-status returned no JSON"}
    if not isinstance(data, dict):
        return {"ok": False, "devices": [], "detail": "decklink-status returned no JSON"}
    if data.get("error"):
        return {"ok": False, "devices": [], "detail": str(data["error"])}
    devices = data.get("devices")
    if not isinstance(devices, list):
        return {"ok": False, "devices": [], "detail": "decklink-status returned no devices"}
    return {"ok": True, "devices": devices, "detail": ""}


class DeckLinkWatch:
    """Cache decklink-status so the operator page does not probe the card itself."""

    def __init__(self, helper: str = STATUS_HELPER, runner=None) -> None:
        self.helper = helper
        self.runner = runner
        self._lock = threading.Lock()
        self._payload: dict[str, Any] = {"ok": False, "devices": [], "detail": "no reading yet", "ts": 0.0}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="nexencoder-decklink", daemon=True)
        self._thread.start()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._payload)

    def refresh(self) -> None:
        reading = query_status(self.helper, self.runner)
        reading["ts"] = time.time() if reading.get("ok") else 0.0
        with self._lock:
            self._payload = reading

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:
                with self._lock:
                    self._payload = {"ok": False, "devices": [], "detail": "decklink-status failed", "ts": 0.0}
            self._stop.wait(STATUS_INTERVAL)
