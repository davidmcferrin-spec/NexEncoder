"""Token-authenticated NOC agent.

The operator console on 127.0.0.1 stays unchanged and unauthenticated. This
process is the one NexNOC calls over the WAN. Every route under /api/noc/v1
requires a bearer token. Unit control is limited to the two channel units.
Recalling or clearing a stored effect uses the same filter path as the console.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import ssl
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, parse_qs, unquote, urlencode, urlsplit, urlunsplit, urlparse

from . import __version__
from .audit import try_record
from .cgfeed import detect_html_backend
from .channels import CHANNELS, INPUT_SRT_PORTS, hls_publish_dir, load_config, srt_stats_path
from .effects import _load_live, load_presets, normalize_channel, recall_effect
from .hoststats import HostCollector, parse_uptime
from .pipeline import (
    AUDIO_BITRATE_BPS,
    AUDIO_CHANNELS,
    AUDIO_RATE_HZ,
    HLS_SEGMENT_SECONDS,
    SRT_LATENCY_MS,
    preview_path,
    whep_url,
)
from .signal import signal_mode
from .srtstats import empty_srt, load_srt_snapshot
from .status import parse_ffmpeg_progress, progress_bitrate_bps, signals_from_progress

log = logging.getLogger("nexencoder.noc")

API_VERSION = "v1"
API_PREFIX = ("api", "noc", "v1")
EXIT_CONFIG = 78
MODEL = "NexEncoder"
CONTROL_ACTIONS = frozenset({"start", "stop", "restart"})
CHANNEL_UNITS = {channel_id: f"nexencoder-channel@{channel_id}.service" for channel_id in CHANNELS}
PROGRESS_FRESH_SECONDS = 8.0
PLAYLIST_FRESH_SECONDS = 15.0
SRT_FRESH_SECONDS = 30.0
_SECRET_QUERY = frozenset({
    "token", "key", "secret", "password", "pass", "signature", "sig",
    "auth", "access_token", "passphrase", "api_key", "apikey",
})
_SECRET_QUERY_NAMES = _SECRET_QUERY | {"token"}
_NAME_RE = re.compile(
    r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?"
    r"(?:\.[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?)*"
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9._~+/-]{16,256}")
_LABEL_RE = re.compile(r"[^\x00-\x1f]{1,40}")
_FAIL_RESULTS = frozenset({
    "exit-code", "timeout", "signal", "core-dump", "watchdog", "start-limit-hit",
})

DEFAULT_NOC: dict[str, object] = {
    "encoder_id": "",
    "site": "",
    "location": "",
    "listen": "0.0.0.0",
    "port": 8790,
    "allow": [],
    "preview_host": "",
    "preview_port": 8889,
    "preview_scheme": "http",
    "tls_cert": "",
    "tls_key": "",
    "token_file": "/etc/nexencoder/noc.token",
    "use_sudo": False,
    "trust_proxy": False,
    "hls_root": "/var/lib/nexencoder/hls",
    "encoder_config": "/var/lib/nexencoder/config.json",
    "state_dir": "/var/lib/nexencoder/state",
}


class NocError(Exception):
    def __init__(self, status: int, error: str, detail: str = "") -> None:
        super().__init__(detail or error)
        self.status = status
        self.error = error
        self.detail = detail


def tokens_match(provided: str, expected: str) -> bool:
    """Compare tokens without leaking the expected length or a prefix."""
    left = hashlib.sha256(provided.encode("utf-8")).digest()
    right = hashlib.sha256(expected.encode("utf-8")).digest()
    same = hmac.compare_digest(left, right)
    return bool(expected) and bool(provided) and same


def bearer_token(header: str | None) -> str | None:
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    if not token or any(char in token for char in "\r\n "):
        return None
    return token


def ip_allowed(ip: str, allow: list[str]) -> bool:
    """An empty allowlist permits every address. The token is still required."""
    if not allow:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for item in allow:
        try:
            if "/" in item:
                if addr in ipaddress.ip_network(item, strict=False):
                    return True
            elif addr == ipaddress.ip_address(item):
                return True
        except ValueError:
            continue
    return False


def redact_url(url: str) -> str:
    """Drop userinfo passwords and query secrets. The path and host stay."""
    if not url:
        return ""
    parts = urlsplit(url)
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, host = netloc.rsplit("@", 1)
        if ":" in userinfo:
            user, _password = userinfo.split(":", 1)
            userinfo = f"{user}:REDACTED"
        else:
            userinfo = "REDACTED"
        netloc = f"{userinfo}@{host}"
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in _SECRET_QUERY:
            query.append((key, "REDACTED"))
        else:
            query.append((key, value))
    return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), ""))


def _label(value: object, limit: int = 200) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("\r", " ").replace("\n", " ").strip()[:limit]


def _port(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    if 1 <= value <= 65535:
        return value
    return default


def _listen(value: object) -> str:
    if not isinstance(value, str):
        return "0.0.0.0"
    text = value.strip()
    if text == "localhost":
        return "127.0.0.1"
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return "0.0.0.0"
    return text


def _flag(value: object) -> bool:
    return value is True


def _clean_allow(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    kept: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        try:
            if "/" in text:
                ipaddress.ip_network(text, strict=False)
            else:
                ipaddress.ip_address(text)
        except ValueError:
            log.warning("ignoring allow entry %r", text)
            continue
        kept.append(text)
    return kept


def load_noc_config(path: Path) -> dict:
    """Load /etc/nexencoder/noc.json. Missing or broken files use defaults."""
    raw: dict = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        loaded = None
    if isinstance(loaded, dict):
        raw = loaded
    scheme = raw.get("preview_scheme")
    if scheme not in {"http", "https"}:
        scheme = "http"
    config = {
        "encoder_id": _label(raw.get("encoder_id")),
        "site": _label(raw.get("site")),
        "location": _label(raw.get("location")),
        "listen": _listen(raw.get("listen", "0.0.0.0")),
        "port": _port(raw.get("port"), 8790),
        "allow": _clean_allow(raw.get("allow")),
        "preview_host": _label(raw.get("preview_host"), 253),
        "preview_port": _port(raw.get("preview_port"), 8889),
        "preview_scheme": scheme,
        "tls_cert": _label(raw.get("tls_cert"), 500),
        "tls_key": _label(raw.get("tls_key"), 500),
        "token_file": _label(raw.get("token_file"), 500) or str(DEFAULT_NOC["token_file"]),
        "use_sudo": _flag(raw.get("use_sudo")),
        "trust_proxy": _flag(raw.get("trust_proxy")),
        "hls_root": _label(raw.get("hls_root"), 500) or str(DEFAULT_NOC["hls_root"]),
        "encoder_config": _label(raw.get("encoder_config"), 500) or str(DEFAULT_NOC["encoder_config"]),
        "state_dir": _label(raw.get("state_dir"), 500) or str(DEFAULT_NOC["state_dir"]),
    }
    if "web" in Path(config["token_file"]).parts:
        log.warning("token file %s is inside a web directory", config["token_file"])
    return config


def effect_slug(name: str) -> str:
    """Lowercase path segment. Spaces and other separators become one underscore."""
    cleaned: list[str] = []
    underscore = False
    for ch in name.strip().lower():
        if ch.isascii() and (ch.isalnum() or ch == "-"):
            cleaned.append(ch)
            underscore = False
        elif cleaned and not underscore:
            cleaned.append("_")
            underscore = True
        else:
            underscore = bool(cleaned)
    return "".join(cleaned).strip("_")


def _token_record(item: dict) -> dict | None:
    token = item.get("token")
    label = item.get("label")
    state = item.get("state")
    ident = item.get("id")
    created = item.get("created")
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        return None
    if not isinstance(ident, str) or not ident.strip() or len(ident) > 40:
        return None
    if not isinstance(label, str) or not _LABEL_RE.fullmatch(label.strip()):
        return None
    if state not in {"active", "revoked"}:
        return None
    if isinstance(created, bool) or not isinstance(created, int) or created < 0:
        created = 0
    return {
        "id": ident.strip(),
        "label": label.strip(),
        "state": state,
        "created": created,
        "token": token,
    }


def load_token_records(path: Path) -> list[dict]:
    """Active and revoked tokens. A file of bare token lines stays valid."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    records: list[dict] = []
    legacy = 0
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        if raw.startswith("{"):
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                record = _token_record(item)
                if record is not None:
                    records.append(record)
            continue
        if _TOKEN_RE.fullmatch(raw):
            legacy += 1
            records.append({
                "id": "legacy" if legacy == 1 else f"legacy-{legacy}",
                "label": "NexNOC" if legacy == 1 else f"Token {legacy}",
                "state": "active",
                "created": 0,
                "token": raw,
            })
    return records


def read_token(path: Path) -> str:
    for record in load_token_records(path):
        if record["state"] == "active":
            return record["token"]
    return ""


def public_token(record: dict) -> dict:
    token = record["token"]
    return {
        "id": record["id"],
        "label": record["label"],
        "state": record["state"],
        "created": record["created"],
        "last4": token[-4:],
    }


def save_token_records(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(body, encoding="utf-8")
    temporary.replace(path)
    try:
        path.chmod(0o640)
    except OSError:
        pass
    try:
        import grp

        os.chown(path, 0, grp.getgrnam("nexencoder").gr_gid)
    except (ImportError, KeyError, OSError, AttributeError):
        pass


def list_api_tokens(path: Path) -> list[dict]:
    return [public_token(record) for record in load_token_records(path)]


def _fresh_id(records: list[dict]) -> str:
    used = {record["id"] for record in records}
    ident = secrets.token_hex(4)
    while ident in used:
        ident = secrets.token_hex(4)
    return ident


def create_api_token(path: Path, label: str, now: int | None = None) -> dict:
    text = label.strip() if isinstance(label, str) else ""
    if not _LABEL_RE.fullmatch(text):
        raise ValueError("label must be 1 to 40 characters")
    records = load_token_records(path)
    record = {
        "id": _fresh_id(records),
        "label": text,
        "state": "active",
        "created": int(time.time() if now is None else now),
        "token": secrets.token_hex(32),
    }
    records.append(record)
    save_token_records(path, records)
    shown = public_token(record)
    shown["secret"] = record["token"]
    return shown


def revoke_api_token(path: Path, token_id: str) -> None:
    records = load_token_records(path)
    found = False
    for record in records:
        if record["id"] == token_id:
            record["state"] = "revoked"
            found = True
    if not found:
        raise ValueError("unknown token")
    save_token_records(path, records)


def delete_api_token(path: Path, token_id: str) -> None:
    records = load_token_records(path)
    kept = [record for record in records if record["id"] != token_id]
    if len(kept) == len(records):
        raise ValueError("unknown token")
    save_token_records(path, kept)


def token_world_readable(path: Path) -> str | None:
    try:
        mode = path.stat().st_mode
    except OSError:
        return None
    if mode & 0o004:
        return f"{path} is world-readable; chmod 640 and group it to the noc user"
    return None


def tls_context(cert: str, key: str) -> ssl.SSLContext:
    if not cert or not key:
        raise ValueError("tls_cert and tls_key are both required")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    return ctx


def normalize_outputs(value: object) -> tuple[str, ...]:
    if value in (None, "all", "*"):
        return tuple(CHANNELS)
    if isinstance(value, str):
        items: list[object] = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise NocError(400, "bad_request", "outputs must be A, B, or all")
    if not items:
        raise NocError(400, "bad_request", "outputs must be A, B, or all")
    seen: list[str] = []
    for item in items:
        if item not in CHANNELS:
            raise NocError(400, "bad_request", "unknown output")
        if item not in seen:
            seen.append(str(item))
    return tuple(seen)


def parse_show(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key] = value
    return fields


def parse_unix_timestamp(value: str) -> float | None:
    text = (value or "").strip()
    if not text or text.lower() in {"n/a", "0"}:
        return None
    if text.startswith("@"):
        text = text[1:]
    try:
        number = float(text)
    except ValueError:
        return None
    if number <= 0:
        return None
    if number > 10**14:
        number /= 1_000_000
    elif number > 10**11:
        number /= 1000
    return number


def service_uptime(active: str, started: float | None, now: float) -> int | None:
    if active != "active" or started is None:
        return None
    delta = now - started
    if delta < 0:
        return None
    return int(delta)


def unit_view(fields: dict[str, str] | None, now: float) -> dict:
    if fields is None:
        return {
            "active_state": "unknown",
            "sub_state": "",
            "result": "",
            "failed": False,
            "service_restarts": None,
            "uptime_seconds": None,
            "uptime_known": False,
        }
    active = fields.get("ActiveState") or "unknown"
    result = fields.get("Result") or ""
    failed = active == "failed" or (result in _FAIL_RESULTS and active != "active")
    restarts = fields.get("NRestarts")
    try:
        service_restarts = int(restarts) if restarts not in (None, "") else None
    except ValueError:
        service_restarts = None
    started = parse_unix_timestamp(fields.get("ActiveEnterTimestamp", ""))
    return {
        "active_state": active,
        "sub_state": fields.get("SubState") or "",
        "result": result,
        "failed": failed,
        "service_restarts": service_restarts,
        "uptime_seconds": service_uptime(active, started, now),
        "uptime_known": started is not None or active != "active",
    }


def _split_proc_addr(field: str, ipv6: bool) -> tuple[str, int]:
    ip_hex, port_hex = field.split(":")
    port = int(port_hex, 16)
    if ipv6:
        if len(ip_hex) != 32:
            raise ValueError(field)
        words = [ip_hex[index:index + 8] for index in range(0, 32, 8)]
        raw = b"".join(bytes.fromhex(word)[::-1] for word in words)
        return str(ipaddress.IPv6Address(raw)), port
    if len(ip_hex) != 8:
        raise ValueError(field)
    return str(ipaddress.IPv4Address(bytes.fromhex(ip_hex)[::-1])), port


def parse_proc_udp(text: str, ipv6: bool = False) -> list[tuple[str, int, str, int, int]]:
    rows = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            local_ip, local_port = _split_proc_addr(parts[1], ipv6)
            remote_ip, remote_port = _split_proc_addr(parts[2], ipv6)
            state = int(parts[3], 16)
        except ValueError:
            continue
        rows.append((local_ip, local_port, remote_ip, remote_port, state))
    return rows


def udp_peer_connected(proc_root: Path, mode: str, port: int) -> bool | None:
    """True when an established UDP socket matches the SRT listener or caller."""
    files = ((proc_root / "net" / "udp", False), (proc_root / "net" / "udp6", True))
    if not any(path.is_file() for path, _ipv6 in files):
        return None
    port = int(port)
    try:
        for path, ipv6 in files:
            if not path.is_file():
                continue
            for _lip, local_port, _rip, remote_port, state in parse_proc_udp(
                path.read_text(encoding="utf-8", errors="replace"), ipv6
            ):
                if state != 1 or remote_port == 0:
                    continue
                if mode == "srt-listener" and local_port == port:
                    return True
                if mode == "srt-caller" and remote_port == port:
                    return True
    except OSError:
        return None
    return False


def read_progress(path: Path, now: float, fresh_seconds: float = PROGRESS_FRESH_SECONDS) -> dict:
    empty = {"fresh": False, "fps": None, "bitrate_bps": None, "dropped": None, "dup": None}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[-16000:]
        mtime = path.stat().st_mtime
    except OSError:
        return empty
    fields = parse_ffmpeg_progress(text)
    try:
        frame = int(float(fields.get("frame") or "0"))
    except ValueError:
        frame = 0
    fresh = (now - mtime) <= fresh_seconds and frame > 0

    def number(key: str, integer: bool = False):
        if not fresh:
            return None
        raw = fields.get(key)
        if raw is None or raw == "N/A" or raw == "":
            return None
        try:
            value = float(raw)
        except ValueError:
            return None
        return int(value) if integer else value

    return {
        "fresh": fresh,
        "fps": number("fps"),
        "bitrate_bps": progress_bitrate_bps(fields) if fresh else None,
        "dropped": number("drop_frames", True),
        "dup": number("dup_frames", True),
    }


def read_runs(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError, ValueError):
        return None


def read_fault(path: Path) -> tuple[str, float | None]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "", None
    if not isinstance(raw, dict) or not isinstance(raw.get("message"), str):
        return "", None
    when = raw.get("unix")
    unix = float(when) if isinstance(when, (int, float)) and not isinstance(when, bool) else None
    return " ".join(raw["message"].split())[:500], unix


def playlist_fresh(path: Path, now: float, window: float = PLAYLIST_FRESH_SECONDS) -> bool | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return (now - mtime) <= window


def connection(
    mode: str,
    encoding: bool,
    srt_fresh: bool,
    udp: bool | None,
    playlist: bool | None,
) -> tuple[bool | None, str]:
    if mode.startswith("srt-"):
        if srt_fresh:
            return True, "srt-stats"
        if udp is True:
            return True, "udp-socket"
        if udp is False:
            return False, "no-peer"
        return None, "unknown"
    if mode == "hls-pull":
        if playlist is True:
            return True, "hls-playlist"
        if playlist is False:
            return False, "hls-playlist-stale"
        return False, "hls-playlist-missing"
    if mode == "hls-push":
        if encoding:
            return True, "hls-push-process"
        return False, "pipeline-down"
    if mode == "sdi":
        if encoding:
            return True, "sdi-play"
        return False, "pipeline-down"
    return None, "unknown"


def output_hostname(spec: dict) -> str:
    """Remote name for an SRT caller or an HLS push. Listeners and local pulls have none."""
    mode = spec.get("mode")
    if mode == "srt-caller":
        url = spec.get("url")
        if isinstance(url, str) and url.strip():
            name = url_hostname(url)
            if name:
                return name
        host = spec.get("host")
        return host.strip() if isinstance(host, str) else ""
    if mode == "hls-push":
        url = spec.get("url")
        return url_hostname(url if isinstance(url, str) else "")
    return ""


def url_hostname(url: str) -> str:
    text = url.strip()
    if not text:
        return ""
    parts = urlsplit(text if "://" in text else "//" + text)
    return (parts.hostname or "").strip()


def _srt_interval_loss(body: dict | None) -> bool:
    if not body:
        return False
    for key in ("packets_lost_interval", "retransmits_interval"):
        value = body.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return True
    return False


def transmission(mode: str, evidence: str, srt_body: dict | None, dns: str) -> tuple[str, str]:
    """Green, orange, or red, plus a short reason the dashboard can show."""
    if dns == "failed":
        return "red", "dns"
    if mode.startswith("srt-"):
        if evidence == "srt-stats":
            if _srt_interval_loss(srt_body):
                return "orange", "loss"
            return "green", "connected"
        if evidence == "udp-socket":
            return "orange", "peer"
        return "red", "down"
    if mode == "hls-pull":
        if evidence == "hls-playlist":
            return "green", "connected"
        return "red", "down"
    if mode == "hls-push":
        if evidence == "hls-push-process":
            return "orange", "sending"
        return "red", "down"
    if mode == "sdi":
        if evidence == "sdi-play":
            return "orange", "playing"
        return "red", "down"
    return "red", "down"


def classify_host(host: str) -> str:
    text = host.strip()
    if not text:
        return "none"
    bare = text[1:-1] if text.startswith("[") and text.endswith("]") else text
    try:
        ipaddress.ip_address(bare)
    except ValueError:
        return "name"
    return "address"


def resolve_addresses(host: str, timeout: float = 2.0) -> list[str]:
    """Addresses for one name. A slow resolver fails instead of holding the page."""
    found: list[list[str]] = []
    errors: list[BaseException] = []

    def work() -> None:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError as exc:
            errors.append(exc)
            return
        addresses: list[str] = []
        for info in infos:
            addr = info[4][0]
            if addr not in addresses:
                addresses.append(addr)
        found.append(addresses)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive() or errors or not found or not found[0]:
        raise OSError(f"could not resolve {host}")
    return found[0]


class NameLookup:
    """Cache encoder DNS so the dashboard poll does not query on every pass."""

    def __init__(self, resolver=None, ttl: float = 30.0) -> None:
        self.resolver = resolver or resolve_addresses
        self.ttl = ttl
        self._rows: dict[str, tuple[float, dict]] = {}

    def lookup(self, host: str, now: float | None = None) -> dict:
        kind = classify_host(host)
        text = host.strip()
        if kind == "none":
            return {"host": "", "dns": "none", "addresses": []}
        if kind == "address":
            bare = text[1:-1] if text.startswith("[") and text.endswith("]") else text
            return {"host": bare, "dns": "address", "addresses": [bare]}
        clock = time.time() if now is None else now
        key = text.lower().rstrip(".")
        cached = self._rows.get(key)
        if cached is not None and clock - cached[0] < self.ttl:
            return cached[1]
        try:
            addresses = list(self.resolver(text))
        except OSError:
            row = {"host": text, "dns": "failed", "addresses": []}
        else:
            row = {"host": text, "dns": "ok", "addresses": addresses} if addresses else {
                "host": text, "dns": "failed", "addresses": [],
            }
        self._rows[key] = (clock, row)
        return row


def output_health(
    config: dict,
    state_dir: Path,
    hls_root: Path,
    proc_root: Path,
    names: NameLookup,
    now: float | None = None,
) -> dict:
    """One row per saved destination. DNS failure turns the dot red."""
    clock = time.time() if now is None else now
    signals = signals_from_progress(state_dir, clock)
    channels: dict[str, list[dict]] = {}
    for channel_id in CHANNELS:
        encoding = bool((signals.get(channel_id) or {}).get("running"))
        rows = []
        for spec in config["outputs"][channel_id]:
            mode = spec["mode"]
            host = output_hostname(spec)
            looked = names.lookup(host, clock)
            srt_body = None
            udp = None
            playlist = None
            if mode.startswith("srt-"):
                loaded = load_srt_snapshot(
                    srt_stats_path(state_dir, channel_id, spec["id"]), clock, SRT_FRESH_SECONDS,
                )
                srt_body = loaded or empty_srt()
                udp = udp_peer_connected(proc_root, mode, int(spec["port"]))
            elif mode == "hls-pull":
                playlist = playlist_fresh(
                    hls_publish_dir(hls_root, channel_id, spec["id"]) / "index.m3u8", clock,
                )
            _connected, evidence = connection(
                mode, encoding, bool(srt_body and srt_body.get("fresh")), udp, playlist,
            )
            state, detail = transmission(mode, evidence, srt_body, looked["dns"])
            rows.append({
                "id": spec["id"],
                "mode": mode,
                "host": looked["host"] or host,
                "port": int(spec["port"]) if mode.startswith("srt-") else None,
                "url": redact_url(spec.get("url") or "") if mode in {"hls-push", "srt-caller"} else "",
                "device": str(spec.get("device") or "") if mode == "sdi" else "",
                "state": state,
                "detail": detail,
                "dns": looked["dns"],
                "addresses": looked["addresses"],
            })
        channels[channel_id] = rows
    return {"ok": True, "channels": channels}


def destination(channel_id: str, spec: dict, hls_root: Path) -> str:
    mode = spec["mode"]
    if mode == "srt-listener":
        return f"srt://0.0.0.0:{int(spec['port'])}?mode=listener"
    if mode == "srt-caller":
        url = spec.get("url") or ""
        if isinstance(url, str) and url.lower().startswith("srt://"):
            return redact_url(url)
        return f"srt://{spec['host']}:{int(spec['port'])}?mode=caller"
    if mode == "hls-push":
        return redact_url(spec.get("url") or "")
    if mode == "hls-pull":
        slot = str(spec.get("id") or "1")
        return str(hls_publish_dir(hls_root, channel_id, slot) / "index.m3u8")
    if mode == "sdi":
        return str(spec.get("device") or "")
    return ""


def transport_mode(config_mode: str) -> tuple[str, str]:
    if config_mode.startswith("srt-"):
        return "srt", config_mode.split("-", 1)[1]
    if config_mode.startswith("hls-"):
        return "hls", config_mode.split("-", 1)[1]
    if config_mode == "sdi":
        return "sdi", "playback"
    return "unknown", config_mode


def safe_host(value: str) -> str | None:
    """Accept a hostname or IP. Extra colons from a Host header are rejected."""
    text = value.strip().rstrip(".")
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text or len(text) > 253 or any(char in text for char in " \t\r\n/@"):
        return None
    try:
        ipaddress.ip_address(text)
    except ValueError:
        if not _NAME_RE.fullmatch(text):
            return None
    return text


def host_only(value: str) -> str:
    text = (value or "").strip()
    if text.startswith("["):
        end = text.find("]")
        return text[1:end] if end > 1 else ""
    if text.count(":") == 1:
        return text.split(":", 1)[0]
    return text


def iso_utc(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dedupe(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


class Systemctl:
    """systemctl for the two channel units. show is unprivileged; act may use sudo."""

    def __init__(self, run=None, use_sudo: bool = False) -> None:
        self.run = run or subprocess.run
        self.use_sudo = use_sudo

    def show(self, unit: str) -> str | None:
        if unit not in CHANNEL_UNITS.values():
            raise NocError(400, "bad_request", "unit is not a channel output")
        argv = [
            "systemctl", "show", unit,
            "-p", "ActiveState", "-p", "SubState", "-p", "Result",
            "-p", "NRestarts", "-p", "ActiveEnterTimestamp",
            "--timestamp=unix",
        ]
        try:
            proc = self.run(argv, capture_output=True, text=True, timeout=3, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if getattr(proc, "returncode", 1) != 0 and not (getattr(proc, "stdout", "") or "").strip():
            return None
        return getattr(proc, "stdout", "") or ""

    def act(self, action: str, unit: str) -> tuple[bool, str]:
        if action not in CONTROL_ACTIONS or unit not in CHANNEL_UNITS.values():
            raise NocError(400, "bad_request", "action is not allowed")
        argv = ["systemctl", action, unit]
        if self.use_sudo:
            argv = ["sudo", "-n", *argv]
        try:
            proc = self.run(argv, capture_output=True, text=True, timeout=8, check=False)
        except subprocess.TimeoutExpired:
            return False, "systemctl timed out"
        except OSError as exc:
            return False, str(exc)
        if getattr(proc, "returncode", 1) != 0:
            detail = (getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "").strip()
            return False, (detail or f"systemctl {action} failed")[:300]
        return True, ""


class Agent:
    def __init__(
        self,
        token: str,
        noc: dict,
        encoder_config: Path,
        state_dir: Path,
        hls_root: Path,
        host: HostCollector | None = None,
        systemctl: Systemctl | None = None,
        proc_root: Path | None = None,
        wall=None,
        hostname: str | None = None,
        html_backend=None,
        token_path: Path | None = None,
    ) -> None:
        self.token = token
        self.token_path = token_path
        self.noc = noc
        self.encoder_config = encoder_config
        self.state_dir = state_dir
        self.hls_root = hls_root
        self.host = host or HostCollector(proc_root=proc_root)
        self.systemctl = systemctl or Systemctl(use_sudo=bool(noc.get("use_sudo")))
        self.proc_root = proc_root or Path("/proc")
        self.wall = wall or time.time
        self.hostname = hostname or socket.gethostname()
        self.allow = list(noc.get("allow") or [])
        self.trust_proxy = bool(noc.get("trust_proxy"))
        self.html_backend = html_backend or detect_html_backend

    def token_matches(self, presented: str) -> bool:
        if not presented:
            return False
        path = self.token_path
        if path is not None:
            try:
                present = path.is_file()
            except OSError:
                present = False
            if present:
                digest = hashlib.sha256(presented.encode("utf-8")).digest()
                matched = False
                for record in load_token_records(path):
                    if record["state"] != "active":
                        continue
                    candidate = hashlib.sha256(record["token"].encode("utf-8")).digest()
                    if hmac.compare_digest(digest, candidate):
                        matched = True
                return matched
        if not self.token:
            return False
        return tokens_match(presented, self.token)

    def identity(self, uptime: int | None) -> dict:
        return {
            "encoder_id": self.noc.get("encoder_id") or self.hostname,
            "hostname": self.hostname,
            "model": MODEL,
            "version": __version__,
            "site": self.noc.get("site") or "",
            "location": self.noc.get("location") or "",
            "uptime_seconds": uptime,
        }

    def _uptime(self) -> int | None:
        try:
            text = (self.proc_root / "uptime").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        value = parse_uptime(text)
        if value is None:
            return None
        return int(value)

    def health(self) -> dict:
        ident = self.identity(self._uptime())
        return {
            "ok": True,
            "api_version": API_VERSION,
            "encoder_id": ident["encoder_id"],
            "hostname": ident["hostname"],
            "version": ident["version"],
            "uptime_seconds": ident["uptime_seconds"],
        }

    def _unit(self, unit: str, now: float) -> dict:
        text = self.systemctl.show(unit)
        if text is None:
            return unit_view(None, now)
        return unit_view(parse_show(text), now)

    def _delivery(self, channel_id: str, spec: dict, encoding: bool, now: float) -> dict:
        """One destination. ``transport`` on the channel stays a copy of the first."""
        config_mode = spec["mode"]
        protocol, transport_name = transport_mode(config_mode)
        srt_body = None
        srt_fresh = False
        udp = None
        playlist = None
        if protocol == "srt":
            loaded = load_srt_snapshot(
                srt_stats_path(self.state_dir, channel_id, spec["id"]), now, SRT_FRESH_SECONDS,
            )
            srt_body = loaded or empty_srt()
            srt_fresh = bool(srt_body.get("fresh"))
            udp = udp_peer_connected(self.proc_root, config_mode, int(spec["port"]))
        elif config_mode == "hls-pull":
            playlist = playlist_fresh(
                hls_publish_dir(self.hls_root, channel_id, spec["id"]) / "index.m3u8", now,
            )
        connected, evidence = connection(config_mode, encoding, srt_fresh, udp, playlist)
        return {
            "id": spec["id"],
            "connected": connected,
            "connected_evidence": evidence,
            "protocol": protocol,
            "mode": transport_name,
            "config_mode": config_mode,
            "destination": destination(channel_id, spec, self.hls_root),
            "latency_ms": SRT_LATENCY_MS if protocol == "srt" else None,
            "segment_seconds": HLS_SEGMENT_SECONDS if protocol == "hls" else None,
            "srt": srt_body,
        }

    def _output(self, channel_id: str, config: dict, now: float, preview_host: str) -> dict:
        spec_in = config["inputs"][channel_id]
        mode_name = spec_in["format"]
        mode = signal_mode(mode_name)
        unit = CHANNEL_UNITS[channel_id]
        view = self._unit(unit, now)
        progress = read_progress(self.state_dir / f"{channel_id}.progress", now)
        if view["active_state"] == "unknown":
            encoding = bool(progress["fresh"])
        else:
            encoding = view["active_state"] == "active" and bool(progress["fresh"])
        deliveries = [
            self._delivery(channel_id, spec, encoding, now)
            for spec in config["outputs"][channel_id]
        ]
        primary = deliveries[0]
        fault, fault_unix = read_fault(self.state_dir / f"{channel_id}.error")
        if not fault and view["failed"] and view["result"] and view["result"] != "success":
            fault = view["result"]
        capture = "ffmpeg-lavfi" if spec_in.get("source") == "srt" else "ffmpeg-decklink"
        protocols = {item["protocol"] for item in deliveries}
        if protocols == {"srt"}:
            egress = "tsduck-srt"
        elif protocols == {"hls"}:
            egress = "ffmpeg-hls"
        else:
            egress = "mixed"
        scheme = self.noc.get("preview_scheme") or "http"
        preview = whep_url(channel_id, preview_host, int(self.noc.get("preview_port") or 8889))
        if scheme == "https" and preview.startswith("http://"):
            preview = "https://" + preview[len("http://"):]
        return {
            "id": channel_id,
            "unit": unit,
            "active_state": view["active_state"],
            "sub_state": view["sub_state"],
            "failed": view["failed"],
            "encoding": encoding,
            "connected": primary["connected"],
            "connected_evidence": primary["connected_evidence"],
            "uptime_seconds": view["uptime_seconds"],
            "uptime_known": view["uptime_known"],
            "service_restarts": view["service_restarts"],
            "pipeline_runs": read_runs(self.state_dir / f"{channel_id}.runs"),
            "last_error": fault,
            "last_error_unix": fault_unix,
            "input": {
                "id": channel_id,
                "source": spec_in.get("source") or "sdi",
                "format": mode_name,
                "device": spec_in.get("device") or "",
                "srt_port": INPUT_SRT_PORTS[channel_id] if spec_in.get("source") == "srt" else None,
            },
            "video": {
                "codec": "h264",
                "encoder": "libx264",
                "profile": "high",
                "bitrate_configured_bps": int(spec_in["bitrate"]) * 1_000_000,
                "bitrate_measured_bps": progress["bitrate_bps"],
                "width": mode.width,
                "height": mode.height,
                "format": mode_name,
                "scan": "interlaced" if mode.interlaced else "progressive",
                "field_rate_hz": round(mode.field_rate_hz, 2),
                "fps_nominal": round(mode.frame_rate_hz, 3),
                "fps_measured": progress["fps"],
                "dropped_frames": progress["dropped"],
                "dup_frames": progress["dup"],
            },
            "audio": {
                "codec": "aac",
                "bitrate_configured_bps": AUDIO_BITRATE_BPS,
                "channels": AUDIO_CHANNELS,
                "sample_rate_hz": AUDIO_RATE_HZ,
                "levels_dbfs": None,
                "levels_available": False,
            },
            "pipeline": {
                "capture": capture,
                "encode": "ffmpeg-libx264",
                "audio_encode": "ffmpeg-aac",
                "scte35": "tsduck-spliceinject",
                "egress": egress,
                "preview": "ffmpeg-rtsp-mediamtx",
                "gstreamer": False,
            },
            "preview": {"path": preview_path(channel_id), "whep": preview},
            "transport": {
                "protocol": primary["protocol"],
                "mode": primary["mode"],
                "config_mode": primary["config_mode"],
                "destination": primary["destination"],
                "latency_ms": primary["latency_ms"],
                "segment_seconds": primary["segment_seconds"],
                "srt": primary["srt"],
            },
            "destinations": deliveries,
            "_systemctl_unknown": view["active_state"] == "unknown",
        }

    def status(self, preview_host: str) -> dict:
        now = self.wall()
        try:
            host = self.host.snapshot()
        except Exception:
            log.exception("host metrics failed")
            host = {
                "cpu_percent": None,
                "load": {"1m": None, "5m": None, "15m": None},
                "memory": {"total_bytes": None, "available_bytes": None, "used_percent": None},
                "temperatures_c": [],
                "disks": [],
                "nics": [],
                "gpu": {"available": False, "source": "none", "devices": [], "detail": "host metrics failed"},
                "uptime_seconds": self._uptime(),
                "limitations": ["host_metrics_failed"],
            }
        config = load_config(self.encoder_config)
        outputs = [self._output(channel_id, config, now, preview_host) for channel_id in CHANNELS]
        limitations = list(host.get("limitations") or [])
        limitations.append("audio_levels_unavailable")
        def _deliveries(item: dict) -> list[dict]:
            found = item.get("destinations")
            return found if isinstance(found, list) and found else [item["transport"]]

        if any(
            delivery["protocol"] == "srt" and not (delivery.get("srt") or {}).get("fresh")
            for item in outputs
            for delivery in _deliveries(item)
        ):
            limitations.append("srt_stats_unavailable")
        if any(
            delivery["config_mode"] == "hls-push"
            for item in outputs
            for delivery in _deliveries(item)
        ):
            limitations.append("hls_push_connected_is_process")
        if any(item["_systemctl_unknown"] for item in outputs):
            limitations.append("systemctl_unavailable")
        if any(item["active_state"] == "active" and not item["uptime_known"] for item in outputs):
            limitations.append("output_uptime_unavailable")
        for item in outputs:
            item.pop("_systemctl_unknown", None)
            item.pop("uptime_known", None)
        uptime = host.get("uptime_seconds")
        if not isinstance(uptime, int):
            uptime = None
        return {
            "ok": True,
            "api_version": API_VERSION,
            "collected_at": iso_utc(now),
            "identity": self.identity(uptime),
            "host": {key: value for key, value in host.items() if key != "limitations"},
            "outputs": outputs,
            "limitations": _dedupe(limitations),
        }

    def control(self, action: str, outputs: object, caller: str) -> dict:
        if action not in CONTROL_ACTIONS:
            log.info("control caller=%s action=%s outputs=%s ok=false detail=rejected", caller, action, outputs)
            try_record(self.state_dir, "noc.control", {
                "caller": caller, "action": action, "outputs": outputs, "ok": False, "detail": "rejected",
            })
            raise NocError(400, "bad_request", "action must be start, stop, or restart")
        try:
            ids = normalize_outputs(outputs)
        except NocError:
            log.info("control caller=%s action=%s outputs=%s ok=false detail=rejected", caller, action, outputs)
            try_record(self.state_dir, "noc.control", {
                "caller": caller, "action": action, "outputs": outputs, "ok": False, "detail": "rejected",
            })
            raise
        now = self.wall()
        results = []
        for channel_id in ids:
            unit = CHANNEL_UNITS[channel_id]
            ok, detail = self.systemctl.act(action, unit)
            view = self._unit(unit, now)
            results.append({
                "id": channel_id,
                "unit": unit,
                "ok": ok,
                "detail": detail,
                "active_state": view["active_state"],
                "sub_state": view["sub_state"],
                "failed": view["failed"],
                "service_restarts": view["service_restarts"],
                "uptime_seconds": view["uptime_seconds"],
            })
        all_ok = all(item["ok"] for item in results)
        summary = "; ".join(item["detail"] for item in results if item["detail"]) or "accepted"
        log.info(
            "control caller=%s action=%s outputs=%s ok=%s detail=%s",
            caller,
            action,
            ",".join(ids),
            str(all_ok).lower(),
            summary,
        )
        try_record(self.state_dir, "noc.control", {
            "caller": caller,
            "action": action,
            "outputs": ids,
            "ok": all_ok,
            "detail": summary,
        })
        return {
            "ok": all_ok,
            "api_version": API_VERSION,
            "action": action,
            "caller": caller,
            "outputs": results,
        }

    def _preset_name(self, name: str) -> str:
        text = unquote(name).strip()
        if not text or len(text) > 64 or any(ord(ch) < 32 or ch in "/\\" for ch in text):
            raise NocError(400, "bad_request", "preset name must be 1 to 64 characters")
        return text

    def _output_id(self, raw: str) -> str:
        text = unquote(raw).strip().upper()
        if text not in CHANNELS:
            raise NocError(400, "bad_request", "output must be A or B")
        return text

    def _slug_matches(self, raw: str) -> tuple[str, list[dict]]:
        slug = effect_slug(unquote(raw))
        if not slug or len(slug) > 64:
            raise NocError(400, "bad_request", "preset name must be 1 to 64 characters")
        matches = [item for item in load_presets(self.state_dir) if effect_slug(item["name"]) == slug]
        return slug, matches

    def _live_label(self, slug: str) -> str:
        for channel_id in CHANNELS:
            live = _load_live(self.state_dir, channel_id)
            current = live.get("preset")
            if isinstance(current, str) and current and effect_slug(current) == slug:
                return current
        return slug

    def _active_channels(self, name: str) -> list[str]:
        slug = effect_slug(name)
        found = []
        for channel_id in CHANNELS:
            live = _load_live(self.state_dir, channel_id)
            current = live.get("preset")
            if not isinstance(current, str) or not current:
                continue
            if current == name or (slug and effect_slug(current) == slug):
                found.append(channel_id)
        return found

    def _effect_warning(self, preset: dict | None) -> str:
        if not preset:
            return ""
        notes = []
        layers = preset.get("layers") or []
        html = any(
            isinstance(layer, dict) and isinstance(layer.get("source"), dict) and layer["source"].get("type") == "html"
            for layer in layers
        )
        if html and self.html_backend() == "none":
            notes.append("HTML renderer is not installed; those layers stay transparent")
        return " ".join(notes)

    def _effect_view(self, name: str, preset: dict | None, slug: str = "") -> dict:
        label = preset["name"] if preset else name
        return {
            "name": label,
            "slug": slug or effect_slug(label),
            "exists": preset is not None,
            "valid": preset is not None,
            "active": self._active_channels(label),
            "format": preset["format"] if preset else "",
            "warning": self._effect_warning(preset),
        }

    def effects(self) -> dict:
        presets = load_presets(self.state_dir)
        by_name = {item["name"]: item for item in presets}
        names = list(by_name)
        for channel_id in CHANNELS:
            live = _load_live(self.state_dir, channel_id)
            preset = live.get("preset")
            if isinstance(preset, str) and preset and preset not in by_name:
                by_name[preset] = None
                names.append(preset)
        return {
            "ok": True,
            "api_version": API_VERSION,
            "effects": [self._effect_view(name, by_name[name]) for name in names],
        }

    def effect_status(self, name: str) -> dict:
        slug, matches = self._slug_matches(name)
        if len(matches) > 1:
            raise NocError(400, "bad_request", "preset slug is not unique")
        preset = matches[0] if matches else None
        label = preset["name"] if preset else self._live_label(slug)
        return {"ok": True, "api_version": API_VERSION, **self._effect_view(label, preset, slug)}

    def effect_on_air(self, channel_raw: str, name: str) -> dict:
        channel_id = self._output_id(channel_raw)
        slug, matches = self._slug_matches(name)
        if len(matches) > 1:
            raise NocError(400, "bad_request", "preset slug is not unique")
        preset = matches[0] if matches else None
        label = preset["name"] if preset else self._live_label(slug)
        return {
            "ok": True,
            "api_version": API_VERSION,
            "channel": channel_id,
            "name": label,
            "slug": slug,
            "exists": preset is not None,
            "valid": preset is not None,
            "active": channel_id in self._active_channels(label),
            "warning": self._effect_warning(preset),
        }

    def effect_recall_path(self, channel_raw: str, name: str, caller: str) -> dict:
        channel_id = self._output_id(channel_raw)
        slug, matches = self._slug_matches(name)
        if len(matches) > 1:
            raise NocError(400, "bad_request", "preset slug is not unique")
        if not matches:
            raise NocError(400, "bad_request", "unknown preset")
        return self.effect_command(channel_id, "recall", {"preset": matches[0]["name"]}, caller)

    def effect_command(self, channel_id: str, action: str, body: dict, caller: str) -> dict:
        channel_id = self._output_id(channel_id)
        if action not in {"recall", "clear"}:
            raise NocError(400, "bad_request", "effect action must be recall or clear")
        config = load_config(self.encoder_config)
        try:
            if action == "clear":
                result = normalize_channel(self.state_dir, channel_id, {}, config)
                preset_name = ""
            else:
                raw_name = body.get("preset")
                if not isinstance(raw_name, str):
                    raise NocError(400, "bad_request", "recall needs a preset name")
                preset_name = self._preset_name(raw_name)
                result = recall_effect(self.state_dir, channel_id, {"preset": preset_name}, config)
        except NocError:
            raise
        except ValueError as exc:
            raise NocError(400, "bad_request", str(exc)) from exc
        match = next((item for item in load_presets(self.state_dir) if item["name"] == preset_name), None) if preset_name else None
        warning = self._effect_warning(match)
        record = {
            "caller": caller,
            "channel": channel_id,
            "preset": preset_name,
            "frames": result.get("frames"),
            "ok": True,
        }
        try_record(self.state_dir, "noc.effect." + action, record)
        payload = {
            "ok": True,
            "api_version": API_VERSION,
            "action": action,
            "channel": channel_id,
            "preset": preset_name,
            "frames": result.get("frames"),
            "active": self._active_channels(preset_name) if preset_name else [],
            "caller": caller,
        }
        if warning:
            payload["warning"] = warning
        if action == "clear":
            payload["normalized"] = True
        return payload


def _query_has_secret(query: str) -> bool:
    for key in parse_qs(query, keep_blank_values=True):
        if key.lower() in _SECRET_QUERY_NAMES:
            return True
    return False


def make_handler(agent: Agent):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def version_string(self) -> str:
            return "nexencoder-noc"

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            log.info("%s %s %s %s", self.address_string(), self.command, urlparse(self.path).path, code)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch("DELETE")

        def _caller_ip(self) -> str:
            peer = self.client_address[0]
            if not agent.trust_proxy:
                return peer
            forwarded = self.headers.get("X-Forwarded-For", "")
            candidate = forwarded.split(",")[0].strip()
            if candidate.startswith("[") and "]" in candidate:
                candidate = candidate[1:candidate.index("]")]
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                return peer
            return candidate

        def _authorize(self) -> str | None:
            caller = self._caller_ip()
            allowed = ip_allowed(caller, agent.allow)
            presented = bearer_token(self.headers.get("Authorization"))
            matched = agent.token_matches(presented or "")
            if not allowed:
                log.warning("noc auth failed caller=%s reason=ip_denied", caller)
                self._json(403, {"ok": False, "error": "forbidden", "detail": "caller address is not allowed"})
                return None
            if not matched:
                reason = "missing_token" if not presented else "bad_token"
                log.warning("noc auth failed caller=%s reason=%s", caller, reason)
                self._json(401, {"ok": False, "error": "unauthorized", "detail": reason})
                return None
            return caller

        def _preview_host(self) -> str:
            configured = safe_host(str(agent.noc.get("preview_host") or ""))
            if configured:
                return configured
            header = safe_host(host_only(self.headers.get("Host", "")))
            return header or agent.hostname

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError as exc:
                raise NocError(400, "bad_request", "Content-Length is not a number") from exc
            if length < 0 or length > 65536:
                raise NocError(400, "bad_request", "body is too large")
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise NocError(400, "bad_request", "body is not JSON") from exc
            if not isinstance(payload, dict):
                raise NocError(400, "bad_request", "body must be a JSON object")
            return payload

        def _known_method(self, method: str, parts: list[str]) -> str | None:
            if parts == ["health"] or parts == ["status"] or parts == ["effects"]:
                return "GET"
            if len(parts) == 2 and parts[0] == "effects":
                return "GET"
            if parts == ["control"]:
                return "POST"
            if len(parts) == 2 and parts[0] == "outputs" and parts[1] in CONTROL_ACTIONS:
                return "POST"
            if len(parts) == 3 and parts[0] == "outputs" and parts[2] in CONTROL_ACTIONS:
                return "POST"
            if (
                len(parts) == 4
                and parts[0] == "outputs"
                and parts[2] == "effects"
                and parts[3] in {"recall", "clear"}
            ):
                return "POST"
            if (
                len(parts) == 4
                and parts[0] == "outputs"
                and parts[2] == "effects"
            ):
                return "GET"
            if (
                len(parts) == 5
                and parts[0] == "outputs"
                and parts[2] == "effects"
                and parts[3] == "recall"
            ):
                return "POST"
            return None

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            if _query_has_secret(parsed.query):
                self._json(400, {
                    "ok": False,
                    "error": "bad_request",
                    "detail": "credentials must be in the Authorization header",
                })
                return
            parts = [item for item in parsed.path.split("/") if item]
            if tuple(parts[:3]) != API_PREFIX:
                self._json(404, {"ok": False, "error": "not_found", "detail": ""})
                return
            caller = self._authorize()
            if caller is None:
                return
            tail = parts[3:]
            try:
                expected = self._known_method(method, tail)
                if expected is None:
                    self._json(404, {"ok": False, "error": "not_found", "detail": ""})
                    return
                if method != expected:
                    self._json(405, {"ok": False, "error": "method_not_allowed", "detail": ""})
                    return
                if tail == ["health"]:
                    self._json(200, agent.health())
                elif tail == ["status"]:
                    self._json(200, agent.status(self._preview_host()))
                elif tail == ["effects"]:
                    self._json(200, agent.effects())
                elif len(tail) == 2 and tail[0] == "effects":
                    self._json(200, agent.effect_status(tail[1]))
                elif tail == ["control"]:
                    body = self._body()
                    action = body.get("action")
                    if not isinstance(action, str):
                        raise NocError(400, "bad_request", "action must be start, stop, or restart")
                    self._finish_control(agent.control(action, body.get("outputs", "all"), caller))
                elif len(tail) == 5 and tail[0] == "outputs" and tail[2] == "effects" and tail[3] == "recall":
                    self._json(200, agent.effect_recall_path(tail[1], tail[4], caller))
                elif (
                    len(tail) == 4
                    and tail[0] == "outputs"
                    and tail[2] == "effects"
                    and tail[3] not in {"recall", "clear"}
                ):
                    self._json(200, agent.effect_on_air(tail[1], tail[3]))
                elif len(tail) == 4 and tail[2] == "effects":
                    self._json(200, agent.effect_command(tail[1], tail[3], self._body(), caller))
                elif len(tail) == 2:
                    self._finish_control(agent.control(tail[1], "all", caller))
                else:
                    self._finish_control(agent.control(tail[2], unquote(tail[1]).upper(), caller))
            except NocError as exc:
                self._json(exc.status, {"ok": False, "error": exc.error, "detail": exc.detail})
            except Exception:
                log.exception("noc request failed")
                self._json(500, {"ok": False, "error": "internal", "detail": ""})

        def _finish_control(self, result: dict) -> None:
            self._json(200 if result["ok"] else 502, result)

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(agent: Agent, host: str, port: int, tls: ssl.SSLContext | None = None) -> ThreadingHTTPServer:
    family = socket.AF_INET
    try:
        if isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address):
            family = socket.AF_INET6
    except ValueError:
        family = socket.AF_INET

    class Bound(_Server):
        address_family = family

    httpd = Bound((host, port), make_handler(agent))
    if tls is not None:
        httpd.socket = tls.wrap_socket(httpd.socket, server_side=True)
    return httpd


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="NexEncoder NOC agent")
    parser.add_argument("--config", type=Path, default=Path("/etc/nexencoder/noc.json"))
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--encoder-config", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--hls", type=Path)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args(argv)
    noc = load_noc_config(args.config)
    if args.token_file is not None:
        noc["token_file"] = str(args.token_file)
    if args.encoder_config is not None:
        noc["encoder_config"] = str(args.encoder_config)
    if args.state is not None:
        noc["state_dir"] = str(args.state)
    if args.hls is not None:
        noc["hls_root"] = str(args.hls)
    if args.host:
        noc["listen"] = _listen(args.host)
    if args.port is not None:
        noc["port"] = _port(args.port, int(noc["port"]))
    token_path = Path(str(noc["token_file"]))
    token = read_token(token_path)
    if not _TOKEN_RE.fullmatch(token or ""):
        log.error("NOC token file is missing, empty, or not 16-256 token characters: %s", token_path)
        return EXIT_CONFIG
    warning = token_world_readable(token_path)
    if warning:
        log.warning("%s", warning)
    agent = Agent(
        token,
        noc,
        Path(str(noc["encoder_config"])),
        Path(str(noc["state_dir"])),
        Path(str(noc["hls_root"])),
        token_path=token_path,
    )
    tls = None
    if noc["tls_cert"] or noc["tls_key"]:
        try:
            tls = tls_context(str(noc["tls_cert"]), str(noc["tls_key"]))
        except (OSError, ssl.SSLError, ValueError) as exc:
            log.error("TLS configuration failed: %s", exc)
            return EXIT_CONFIG
    host = str(noc["listen"])
    port = int(noc["port"])
    if tls is None and host not in {"127.0.0.1", "::1"}:
        log.warning(
            "NOC agent on %s:%s has no TLS. Terminate TLS at a reverse proxy or set tls_cert and tls_key.",
            host,
            port,
        )
    try:
        httpd = serve(agent, host, port, tls)
    except OSError as exc:
        log.error("could not bind %s:%s: %s", host, port, exc)
        return 1
    scheme = "https" if tls is not None else "http"
    log.info("NOC agent on %s://%s:%s/api/noc/v1/", scheme, host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
