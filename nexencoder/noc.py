"""Token-authenticated NOC agent.

The operator console on 127.0.0.1 stays unchanged and unauthenticated. This
process is the one NexNOC calls over the WAN. Every route under /api/noc/v1
requires a bearer token. Control is limited to the two channel units.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import socket
import ssl
import subprocess
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, parse_qs, urlencode, urlsplit, urlunsplit, urlparse

from . import __version__
from .audit import try_record
from .channels import CHANNELS, INPUT_SRT_PORTS, load_config
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
from .status import parse_ffmpeg_progress, progress_bitrate_bps

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


def read_token(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    for line in text.splitlines():
        token = line.strip()
        if token and not token.startswith("#"):
            return token
    return ""


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
    return None, "unknown"


def destination(channel_id: str, spec: dict, hls_root: Path) -> str:
    mode = spec["mode"]
    if mode == "srt-listener":
        return f"srt://0.0.0.0:{int(spec['port'])}?mode=listener"
    if mode == "srt-caller":
        return f"srt://{spec['host']}:{int(spec['port'])}?mode=caller"
    if mode == "hls-push":
        return redact_url(spec.get("url") or "")
    if mode == "hls-pull":
        return str(hls_root / channel_id / "index.m3u8")
    return ""


def transport_mode(config_mode: str) -> tuple[str, str]:
    if config_mode.startswith("srt-"):
        return "srt", config_mode.split("-", 1)[1]
    if config_mode.startswith("hls-"):
        return "hls", config_mode.split("-", 1)[1]
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
    ) -> None:
        self.token = token
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

    def _output(self, channel_id: str, config: dict, now: float, preview_host: str) -> dict:
        spec_in = config["inputs"][channel_id]
        spec_out = config["outputs"][channel_id]
        mode_name = spec_in["format"]
        mode = signal_mode(mode_name)
        unit = CHANNEL_UNITS[channel_id]
        view = self._unit(unit, now)
        progress = read_progress(self.state_dir / f"{channel_id}.progress", now)
        if view["active_state"] == "unknown":
            encoding = bool(progress["fresh"])
        else:
            encoding = view["active_state"] == "active" and bool(progress["fresh"])
        config_mode = spec_out["mode"]
        protocol, transport_name = transport_mode(config_mode)
        srt_body = None
        srt_fresh = False
        udp = None
        playlist = None
        if protocol == "srt":
            loaded = load_srt_snapshot(self.state_dir / f"{channel_id}.srt.json", now, SRT_FRESH_SECONDS)
            srt_body = loaded or empty_srt()
            srt_fresh = bool(srt_body.get("fresh"))
            udp = udp_peer_connected(self.proc_root, config_mode, int(spec_out["port"]))
        elif config_mode == "hls-pull":
            playlist = playlist_fresh(self.hls_root / channel_id / "index.m3u8", now)
        connected, evidence = connection(config_mode, encoding, srt_fresh, udp, playlist)
        fault, fault_unix = read_fault(self.state_dir / f"{channel_id}.error")
        if not fault and view["failed"] and view["result"] and view["result"] != "success":
            fault = view["result"]
        capture = "ffmpeg-lavfi" if spec_in.get("source") == "srt" else "ffmpeg-decklink"
        egress = "tsduck-srt" if protocol == "srt" else "ffmpeg-hls"
        scheme = self.noc.get("preview_scheme") or "http"
        preview = whep_url(channel_id, preview_host, int(self.noc.get("preview_port") or 8889))
        if scheme == "https" and preview.startswith("http://"):
            preview = "https://" + preview[len("http://"):]
        latency = SRT_LATENCY_MS if protocol == "srt" else None
        segment = HLS_SEGMENT_SECONDS if protocol == "hls" else None
        return {
            "id": channel_id,
            "unit": unit,
            "active_state": view["active_state"],
            "sub_state": view["sub_state"],
            "failed": view["failed"],
            "encoding": encoding,
            "connected": connected,
            "connected_evidence": evidence,
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
                "bitrate_configured_bps": int(spec_out["bitrate"]) * 1_000_000,
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
                "protocol": protocol,
                "mode": transport_name,
                "config_mode": config_mode,
                "destination": destination(channel_id, spec_out, self.hls_root),
                "latency_ms": latency,
                "segment_seconds": segment,
                "srt": srt_body,
            },
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
        if any(item["transport"]["protocol"] == "srt" and not (item["transport"]["srt"] or {}).get("fresh") for item in outputs):
            limitations.append("srt_stats_unavailable")
        if any(item["transport"]["config_mode"] == "hls-push" for item in outputs):
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
            matched = tokens_match(presented or "", agent.token)
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
            if parts == ["health"] or parts == ["status"]:
                return "GET"
            if parts == ["control"]:
                return "POST"
            if len(parts) == 2 and parts[0] == "outputs" and parts[1] in CONTROL_ACTIONS:
                return "POST"
            if len(parts) == 3 and parts[0] == "outputs" and parts[2] in CONTROL_ACTIONS:
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
                elif tail == ["control"]:
                    body = self._body()
                    action = body.get("action")
                    if not isinstance(action, str):
                        raise NocError(400, "bad_request", "action must be start, stop, or restart")
                    self._finish_control(agent.control(action, body.get("outputs", "all"), caller))
                elif len(tail) == 2:
                    self._finish_control(agent.control(tail[1], "all", caller))
                else:
                    self._finish_control(agent.control(tail[2], tail[1], caller))
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
