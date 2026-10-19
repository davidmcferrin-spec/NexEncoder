"""Allowlisted systemd control for the encoder host.

The console may start, stop, restart, enable, and disable only the units
listed here. Names that are not on the list are rejected before any command
runs.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Callable

ALLOWED_UNITS: tuple[str, ...] = (
    "nexencoder-console.service",
    "prometheus.service",
    "prometheus-node-exporter.service",
    "mediamtx.service",
    "nexencoder-channel@A.service",
    "nexencoder-channel@B.service",
)
ACTIONS = frozenset({"start", "stop", "restart", "enable", "disable"})
_UNIT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._:-]{0,127}")

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class ServiceError(ValueError):
    """The requested unit or action is not allowed, or systemctl refused it."""


class ServiceUnavailable(RuntimeError):
    """systemctl or journalctl is not on this host."""


def normalize_unit(name: str) -> str:
    raw = (name or "").strip()
    if raw.endswith(".service"):
        candidate = raw
    else:
        candidate = raw + ".service"
    if not _UNIT_RE.fullmatch(candidate) or candidate not in ALLOWED_UNITS:
        raise ServiceError(f"{name!r} is not an encoder service")
    return candidate


def _privileged(argv: list[str]) -> list[str]:
    if os.name != "posix":
        return argv
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and geteuid() != 0:
        return ["sudo", "-n", *argv]
    return argv


def default_run(args: list[str]) -> subprocess.CompletedProcess[str]:
    binary = "sudo" if _privileged(["systemctl"])[0] == "sudo" else "systemctl"
    if shutil.which(binary) is None or shutil.which("systemctl") is None:
        raise ServiceUnavailable("systemctl is not available on this host")
    try:
        return subprocess.run(
            _privileged(["systemctl", *args]),
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ServiceUnavailable("systemctl timed out") from exc


def _parse_show(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key] = value
    return fields


def list_units(run: Runner | None = None) -> dict[str, object]:
    runner = run or default_run
    units: list[dict[str, str]] = []
    try:
        for name in ALLOWED_UNITS:
            proc = runner([
                "show", name,
                "-p", "ActiveState",
                "-p", "SubState",
                "-p", "UnitFileState",
                "-p", "Description",
            ])
            fields = _parse_show(proc.stdout or "")
            units.append({
                "name": name,
                "active": fields.get("ActiveState") or "unknown",
                "sub": fields.get("SubState") or "",
                "enabled": fields.get("UnitFileState") or "",
                "description": fields.get("Description") or "",
                "error": "" if proc.returncode == 0 else (proc.stderr or "").strip(),
            })
    except ServiceUnavailable as exc:
        return {
            "ok": True,
            "systemctl": False,
            "detail": str(exc),
            "units": [
                {"name": name, "active": "unavailable", "sub": "", "enabled": "", "description": "", "error": ""}
                for name in ALLOWED_UNITS
            ],
        }
    return {"ok": True, "systemctl": True, "detail": "", "units": units}


def act(unit: str, action: str, run: Runner | None = None) -> dict[str, object]:
    name = normalize_unit(unit)
    if action not in ACTIONS:
        raise ServiceError(f"{action!r} is not an allowed action")
    runner = run or default_run
    proc = runner([action, name])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"systemctl {action} failed"
        raise ServiceError(detail)
    return {"ok": True, "unit": name, "action": action}


def journal_argv(unit: str, follow: bool, since: str | None = None) -> list[str]:
    name = normalize_unit(unit)
    if shutil.which("journalctl") is None:
        raise ServiceUnavailable("journalctl is not available on this host")
    cmd = ["journalctl", "-u", name, "-o", "short-iso", "--no-pager"]
    if since:
        cmd.extend(["--since", since])
    else:
        cmd.extend(["-n", "200"])
    if follow:
        cmd.append("-f")
    return _privileged(cmd)
