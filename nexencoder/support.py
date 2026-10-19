"""Redacted support zip for the encoder host.

The zip holds host facts, the saved encoder config, and journals for the
encoder units. URL credentials and password-like fields are replaced. The
NOC token file is never read.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import socket
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .services import _privileged

SUPPORT_HOURS = ("24", "36", "48", "72", "all")
JOURNAL_UNITS: tuple[str, ...] = (
    "nexencoder-console.service",
    "nexencoder-channel@A.service",
    "nexencoder-channel@B.service",
    "nexencoder-noc.service",
    "nexencoder-decklink-configure.service",
    "mediamtx.service",
    "prometheus.service",
    "prometheus-node-exporter.service",
    "apache2.service",
)
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
DMESG_TAIL = 400
DUMP_FILENAME = "last-journal-dump.zip"
_CLEAR_COMMANDS: tuple[list[str], ...] = (
    ["journalctl", "--rotate"],
    ["journalctl", "--vacuum-time=1s"],
)
HOST_COMMANDS: tuple[tuple[str, list[str]], ...] = (
    ("uname.txt", ["uname", "-a"]),
    ("uptime.txt", ["uptime"]),
    ("free.txt", ["free", "-h"]),
    ("df.txt", ["df", "-h"]),
    ("ip.txt", ["ip", "-br", "a"]),
    ("lsblk.txt", ["lsblk", "-o", "NAME,SIZE,TYPE,MOUNTPOINT"]),
    ("lsmod.txt", ["lsmod"]),
    ("lspci.txt", ["lspci", "-nn"]),
    ("timedatectl.txt", ["timedatectl"]),
    ("lscpu.txt", ["lscpu"]),
    ("python3.txt", ["python3", "--version"]),
    ("ffmpeg.txt", ["ffmpeg", "-version"]),
    ("tsp.txt", ["tsp", "--version"]),
    ("php.txt", ["php", "-v"]),
)
SYSTEM_COMMANDS: tuple[tuple[str, list[str]], ...] = (
    ("list-units.txt", [
        "systemctl", "list-units",
        "nexencoder*", "mediamtx*", "apache2*", "prometheus*",
        "--all", "--no-pager",
    ]),
)

Runner = Callable[[list[str]], tuple[int, str, str]]

_URL_CRED = re.compile(r"(?i)((?:srt|rtsp|rtsps|https?|udp)://)([^/\s:@]+):([^/\s@]+)@")
_SECRET = re.compile(
    r'(?i)("?(?:password|passwd|passphrase|secret|token|api_key|authorization)"?\s*[=:]\s*"?)([^"\s,}]+)'
)
_HOST = re.compile(r"[^A-Za-z0-9._-]+")


def parse_hours(value: str) -> int | None:
    raw = (value or "").strip().lower()
    if raw == "all":
        return None
    if raw not in ("24", "36", "48", "72"):
        raise ValueError("hours must be 24, 36, 48, 72, or all")
    return int(raw)


def redact(text: str) -> str:
    text = _URL_CRED.sub(r"\1\2:REDACTED@", text)
    return _SECRET.sub(lambda match: match.group(1) + "REDACTED", text)


def journal_command(unit: str, hours: int | None) -> list[str]:
    cmd = ["journalctl", "-u", unit, "-o", "short-iso", "--no-pager"]
    if hours is not None:
        cmd.extend(["--since", f"{hours} hours ago"])
    return cmd


def _default_runner(argv: list[str]) -> tuple[int, str, str]:
    cmd = list(argv)
    if cmd and cmd[0] in {"journalctl", "systemctl", "dmesg"}:
        cmd = _privileged(cmd)
    binary = cmd[0] if cmd else ""
    if not binary or shutil.which(binary) is None:
        return 127, "", f"{argv[0] if argv else 'command'}: not installed\n"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _cap(text: str, limit: int | None = MAX_JOURNAL_BYTES) -> str:
    if limit is None:
        return text
    raw = text.encode("utf-8", "replace")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", "ignore") + "\n... truncated\n"


def _one_line(text: str) -> str:
    cleaned = "".join(ch for ch in " ".join(text.split()) if 32 <= ord(ch) <= 126)
    return cleaned[:180]


def _host_label(name: str) -> str:
    cleaned = _HOST.sub("-", name).strip("-")
    return cleaned[:40] or "host"


def _capture(run: Runner, argv: list[str]) -> str:
    code, stdout, stderr = run(argv)
    text = stdout if stdout.strip() else (stderr or f"{argv[0]} exited {code}\n")
    if code != 0 and stdout.strip() and stderr.strip():
        text += "\n" + stderr
    return _cap(redact(text))


def _dmesg(run: Runner) -> str:
    text = _capture(run, ["dmesg", "-T", "--level=err,warn"])
    lines = text.splitlines()
    if len(lines) > DMESG_TAIL:
        text = "\n".join(lines[-DMESG_TAIL:]) + "\n"
    return text


def build_bundle(
    hours: str,
    *,
    config_path: Path | None = None,
    runner: Runner | None = None,
    hostname: str = "",
    now: datetime | None = None,
    journal_limit: int | None = MAX_JOURNAL_BYTES,
) -> tuple[str, bytes]:
    window = parse_hours(hours)
    run = runner or _default_runner
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    host = _host_label(hostname or socket.gethostname())
    label = "all" if window is None else f"{window}h"
    filename = f"nexencoder-support-{stamp}-{host}-{label}.zip"
    notes: list[str] = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, argv in HOST_COMMANDS:
            archive.writestr(f"host/{name}", _capture(run, argv))
        archive.writestr("host/dmesg-warn-err.txt", _dmesg(run))
        archive.writestr("host/decklink-status.txt", _capture(run, ["/usr/local/bin/decklink-status"]))
        archive.writestr("host/decklink-configure.txt", _capture(run, ["/usr/local/bin/decklink-configure", "--status"]))
        for name, argv in SYSTEM_COMMANDS:
            archive.writestr(f"systemd/{name}", _capture(run, argv))
        for unit in JOURNAL_UNITS:
            archive.writestr(
                f"systemd/status/{unit}.txt",
                _capture(run, ["systemctl", "status", unit, "--no-pager", "-l"]),
            )
        if config_path is not None and config_path.is_file():
            archive.writestr("config/encoder.json", redact(config_path.read_text(encoding="utf-8")))
        else:
            notes.append("encoder config was not on disk")
        for unit in JOURNAL_UNITS:
            code, stdout, stderr = run(journal_command(unit, window))
            text = stdout if stdout.strip() else (stderr or f"journalctl exited {code}\n")
            if code != 0 and stdout.strip():
                text = text + "\n" + (stderr or f"journalctl exited {code}\n")
            archive.writestr(f"journal/{unit}.log", _cap(redact(text), journal_limit))
        readme = (
            "NexEncoder support bundle\n"
            f"Window: {'all retained journal lines' if window is None else f'last {window} hours'}\n"
            f"Generated: {stamp}\n"
            "Includes every encoder service journal, systemd status, lsmod, dmesg warnings, and DeckLink status.\n"
            "URL credentials and password-like fields are redacted. The NOC token is not included.\n"
        )
        if notes:
            readme += "\n".join(notes) + "\n"
        archive.writestr("README.txt", readme)
        archive.writestr("manifest.json", json.dumps({
            "hours": "all" if window is None else window,
            "units": list(JOURNAL_UNITS),
            "generated": stamp,
        }, indent=2) + "\n")
    return filename, buffer.getvalue()


def _store_dump(state_dir: Path, data: bytes) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / DUMP_FILENAME
    temporary = state_dir / f".{DUMP_FILENAME}.part"
    temporary.write_bytes(data)
    os.replace(temporary, target)
    return target


def clear_journal(runner: Runner | None = None) -> str:
    """Rotate the host journal and drop archived files. Empty string means cleared."""
    run = runner or _default_runner
    for argv in _CLEAR_COMMANDS:
        code, stdout, stderr = run(argv)
        if code != 0:
            detail = _one_line(stderr or stdout)
            return detail or f"{' '.join(argv)} exited {code}"
    return ""


def dump_and_clear(
    *,
    state_dir: Path,
    config_path: Path | None = None,
    runner: Runner | None = None,
    hostname: str = "",
    now: datetime | None = None,
) -> tuple[str, bytes, str]:
    """Save a full redacted journal zip, then vacuum. The third value is empty when cleared.

    The zip is written under state_dir before any journal file is removed. A failed
    copy leaves the journal alone. A failed vacuum still returns the zip.
    """
    filename, data = build_bundle(
        "all",
        config_path=config_path,
        runner=runner,
        hostname=hostname,
        now=now,
        journal_limit=None,
    )
    _store_dump(state_dir, data)
    return filename, data, clear_journal(runner)
