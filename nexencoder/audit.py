"""Operator audit log and per-unit journal view cutoffs.

There is no login. Each line is a time, an action, and the settings that were
posted. The file lives in the state directory, separate from the host journal,
and Clear All does not delete it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("nexencoder.audit")

AUDIT_FILENAME = "audit.log"
CUTOFF_FILENAME = "journal-cutoffs.json"
_SINCE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC$")
_SECRET_SUFFIXES = ("password", "passwd", "passphrase", "secret", "token", "authorization", "apikey", "credential")
_LOCK = threading.Lock()
_MAX_TEXT = 2000


def record(state_dir: Path, action: str, settings: dict | None = None, *, now: datetime | None = None) -> dict:
    """Append one JSON line. Secret fields and URL credentials are redacted."""
    entry = {
        "time": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "action": str(action),
        "settings": _clean(settings or {}),
    }
    from .support import redact

    line = redact(json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)) + "\n"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / AUDIT_FILENAME
    with _LOCK:
        with path.open("a", encoding="utf-8") as handle:
            _flock(handle)
            handle.write(line)
            handle.flush()
    return entry


def try_record(state_dir: Path, action: str, settings: dict | None = None) -> None:
    """Record an action. A full disk or a bad value does not fail the operator action."""
    try:
        record(state_dir, action, settings)
    except (OSError, TypeError, ValueError) as exc:
        log.warning("audit log was not written: %s", exc)


def read_audit(state_dir: Path) -> str:
    path = state_dir / AUDIT_FILENAME
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def mark_journal_cleared(state_dir: Path, unit: str, *, now: datetime | None = None) -> str:
    """Hide this unit's older journal lines. The stamp is what ``journalctl --since`` accepts."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S UTC")
    with _LOCK:
        current = _cutoffs(state_dir)
        current[unit] = stamp
        _write_cutoffs(state_dir, current)
    return stamp


def journal_since(state_dir: Path, unit: str) -> str | None:
    stamp = _cutoffs(state_dir).get(unit)
    if isinstance(stamp, str) and _SINCE.fullmatch(stamp):
        return stamp
    return None


def clear_journal_cutoffs(state_dir: Path) -> None:
    path = state_dir / CUTOFF_FILENAME
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        _write_cutoffs(state_dir, {})


def _cutoffs(state_dir: Path) -> dict[str, str]:
    path = state_dir / CUTOFF_FILENAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    kept: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(value, str) and _SINCE.fullmatch(value):
            kept[str(key)] = value
    return kept


def _write_cutoffs(state_dir: Path, payload: dict[str, str]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / CUTOFF_FILENAME
    temporary = state_dir / f".{CUTOFF_FILENAME}.part"
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _flock(handle) -> None:
    if os.name != "posix":
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _secret_key(key: object) -> bool:
    folded = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return any(folded == name or folded.endswith(name) for name in _SECRET_SUFFIXES)


def _clean(value: object) -> object:
    if isinstance(value, dict):
        cleaned: dict[str, object] = {}
        for key, item in value.items():
            cleaned[str(key)] = "REDACTED" if _secret_key(key) else _clean(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, str) and len(value) > _MAX_TEXT:
        return value[:_MAX_TEXT] + "…"
    return value
