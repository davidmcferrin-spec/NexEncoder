"""US time zones for the encoder host.

setup.sh leaves a host that is still on UTC in America/New_York. The Services
page can switch among the zones listed here. The bars clock reads
/etc/localtime, so the burned-in abbreviation follows that setting.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

US_TIMEZONES: tuple[tuple[str, str], ...] = (
    ("America/New_York", "Eastern (New York)"),
    ("America/Chicago", "Central (Chicago)"),
    ("America/Denver", "Mountain (Denver)"),
    ("America/Phoenix", "Arizona (Phoenix)"),
    ("America/Los_Angeles", "Pacific (Los Angeles)"),
    ("America/Anchorage", "Alaska (Anchorage)"),
    ("Pacific/Honolulu", "Hawaii (Honolulu)"),
    ("America/Puerto_Rico", "Atlantic (Puerto Rico)"),
    ("Pacific/Pago_Pago", "Samoa (Pago Pago)"),
    ("Pacific/Guam", "Chamorro (Guam)"),
)
ALLOWED = frozenset(zone for zone, _label in US_TIMEZONES)
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class TimezoneError(ValueError):
    """The zone is not on the US list, or timedatectl refused it."""


class TimezoneUnavailable(RuntimeError):
    """timedatectl is not on this host."""


def zone_catalog() -> list[dict[str, str]]:
    return [{"id": zone, "label": label} for zone, label in US_TIMEZONES]


def normalize_timezone(name: str) -> str:
    raw = (name or "").strip()
    if raw not in ALLOWED:
        raise TimezoneError("Choose a US time zone")
    return raw


def abbreviation(zone: str, now: datetime | None = None) -> str:
    """EST, EDT, and the other short names the bars clock can draw."""
    try:
        tz = ZoneInfo(zone)
    except Exception:
        return ""
    moment = now if now is not None else datetime.now(tz)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    else:
        moment = moment.astimezone(tz)
    return moment.tzname() or ""


def default_run(args: list[str]) -> subprocess.CompletedProcess[str]:
    if shutil.which("timedatectl") is None:
        raise TimezoneUnavailable("timedatectl is not available on this host")
    argv = ["timedatectl", *args]
    if args and args[0] == "set-timezone" and os.name == "posix":
        geteuid = getattr(os, "geteuid", None)
        if geteuid is not None and geteuid() != 0:
            if shutil.which("sudo") is None:
                raise TimezoneUnavailable("sudo is not available on this host")
            argv = ["sudo", "-n", *argv]
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimezoneUnavailable("timedatectl timed out") from exc


def current_timezone(run: Runner | None = None) -> dict[str, object]:
    catalog = zone_catalog()
    runner = run or default_run
    try:
        proc = runner(["show", "-p", "Timezone", "--value"])
    except TimezoneUnavailable as exc:
        return {
            "ok": True,
            "timedatectl": False,
            "timezone": "",
            "abbreviation": "",
            "detail": str(exc),
            "zones": catalog,
        }
    zone = (proc.stdout or "").strip()
    if proc.returncode != 0 or not zone:
        detail = (proc.stderr or proc.stdout or "timedatectl failed").strip()
        return {
            "ok": True,
            "timedatectl": False,
            "timezone": "",
            "abbreviation": "",
            "detail": detail[:300],
            "zones": catalog,
        }
    return {
        "ok": True,
        "timedatectl": True,
        "timezone": zone,
        "abbreviation": abbreviation(zone),
        "detail": "" if zone in ALLOWED else "The host zone is not in the US list",
        "zones": catalog,
    }


def set_timezone(name: str, run: Runner | None = None) -> dict[str, object]:
    zone = normalize_timezone(name)
    runner = run or default_run
    proc = runner(["set-timezone", zone])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "timedatectl set-timezone failed").strip()
        raise TimezoneError(detail[:300] or "timedatectl set-timezone failed")
    body = current_timezone(runner)
    body["ok"] = True
    body["timedatectl"] = True
    body["timezone"] = zone
    body["abbreviation"] = abbreviation(zone)
    body["detail"] = ""
    return body
