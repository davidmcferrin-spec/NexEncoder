"""Live data feed for the operator console.

Apache and PHP serve the pages. This process serves /metrics and the /api JSON
routes those pages and the channel supervisor call. It listens on 127.0.0.1 by
default. Service actions are limited to the allowlist in nexencoder.services.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .channels import load_config, save_config, snapshot_for
from .cgfeed import detect_html_backend, ndi_available
from .decklink import list_devices
from .effects import (
    bars_path,
    delete_preset,
    load_presets,
    normalize_channel,
    preset_json,
    read_flag,
    recall_effect,
    still_path,
    upsert_preset,
    write_flag,
)
from .livefeed import check_loop_file
from .errors import ParseError
from .metrics import CATALOG, render_metrics
from .pipeline import SPLICE_PORT, preview_path, sections_from_mom, whep_url
from .prom import PrometheusError, fetch
from .services import ServiceError, ServiceUnavailable, act, journal_argv, list_units, normalize_unit
from .sideband import Sideband
from .status import signals_from_progress
from .vanc import events_from_vanc

log = logging.getLogger("nexencoder.console")

_ACTION_HEADER = "NexEncoder"


class Console:
    def __init__(
        self,
        prometheus_url: str = "http://127.0.0.1:9090",
        unit_runner=None,
        journal_builder=None,
        config_path: Path | None = None,
        state_dir: Path | None = None,
        device_lister=None,
        splice_sender=None,
        sideband: Sideband | None = None,
        html_backend=None,
        format_probe=None,
    ) -> None:
        repo = Path(__file__).resolve().parents[1]
        self.prometheus_url = prometheus_url
        self.unit_runner = unit_runner
        self.journal_builder = journal_builder or journal_argv
        self.config_path = config_path or (repo / "var" / "config.json")
        self.state_dir = state_dir or (repo / "var" / "state")
        self.device_lister = device_lister or list_devices
        self.splice_sender = splice_sender
        self.sideband = sideband or Sideband()
        self.html_backend = html_backend or detect_html_backend
        self.format_probe = format_probe

    def metrics_text(self) -> str:
        config = load_config(self.config_path)
        signals = signals_from_progress(self.state_dir)
        now = time.time()
        for channel_id in ("A", "B"):
            row = signals.setdefault(channel_id, {})
            row["scte104"] = self.sideband.scte_totals[channel_id]
            seen = self.sideband.caption_at[channel_id]
            if seen and now - seen <= 15:
                row["captions"] = True
        return render_metrics(snapshot_for(config, signals))


def _catalog_json() -> list[dict[str, object]]:
    rows = []
    for item in CATALOG:
        row = dict(item)
        queries = row.get("queries")
        if isinstance(queries, tuple):
            row["queries"] = list(queries)
        rows.append(row)
    return rows


def make_handler(console: Console):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:
            log.info("%s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
            try:
                if path == "/metrics":
                    body = console.metrics_text().encode("utf-8")
                    self._bytes(200, "text/plain; version=0.0.4; charset=utf-8", body)
                elif path == "/api/catalog":
                    self._json(200, {"ok": True, "cards": _catalog_json()})
                elif path == "/api/config":
                    config = load_config(console.config_path)
                    bars = {
                        channel_id: read_flag(bars_path(console.state_dir, channel_id), False)
                        for channel_id in ("A", "B")
                    }
                    self._json(200, {"ok": True, "config": config, "bars": bars})
                elif path == "/api/devices":
                    self._json(200, {"ok": True, "devices": console.device_lister()})
                elif path == "/api/preview":
                    host = self.headers.get("Host", "127.0.0.1").split(":")[0] or "127.0.0.1"
                    channels = {
                        channel_id: {"path": preview_path(channel_id), "whep": whep_url(channel_id, host)}
                        for channel_id in ("A", "B")
                    }
                    self._json(200, {"ok": True, "channels": channels})
                elif path == "/api/sideband":
                    self._json(200, {"ok": True, "events": console.sideband.recent()})
                elif path == "/api/effects/presets":
                    self._presets()
                elif path.startswith("/api/channels/") and path.endswith("/still"):
                    channel = unquote(path[len("/api/channels/"):-len("/still")].strip("/"))
                    self._still(channel)
                elif path == "/api/services":
                    self._json(200, list_units(console.unit_runner))
                elif path.startswith("/api/services/") and path.endswith("/log"):
                    unit = unquote(path[len("/api/services/"):-len("/log")].strip("/"))
                    self._journal(unit)
                elif path == "/api/prom/query":
                    self._prom("query", query)
                elif path == "/api/prom/query_range":
                    self._prom("query_range", query)
                elif path == "/api/prom/names":
                    self._prom("names", {})
                else:
                    self._json(404, {"ok": False, "detail": "Not found"})
            except ServiceError as exc:
                self._json(400, {"ok": False, "detail": str(exc)})
            except ServiceUnavailable as exc:
                self._json(503, {"ok": False, "detail": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            if self.headers.get("X-Requested-With") != _ACTION_HEADER:
                self._json(403, {"ok": False, "detail": "Missing action header"})
                return
            parsed = urlparse(self.path)
            parts = [part for part in parsed.path.split("/") if part]
            try:
                body = self._body()
            except ValueError as exc:
                self._json(400, {"ok": False, "detail": str(exc)})
                return
            try:
                if parts == ["api", "config"]:
                    self._save_config(body)
                elif len(parts) == 3 and parts[0] == "api" and parts[1] == "services":
                    action = parse_qs(parsed.query).get("action", [""])[-1]
                    self._json(200, act(unquote(parts[2]), action, console.unit_runner))
                elif parts == ["api", "sideband"]:
                    self._save_sideband(body)
                elif parts == ["api", "effects", "presets"]:
                    self._save_preset(body)
                elif len(parts) == 5 and parts[0] == "api" and parts[1] == "channels" and parts[3] == "effects" and parts[4] == "recall":
                    self._recall(unquote(parts[2]), body)
                elif len(parts) == 5 and parts[0] == "api" and parts[1] == "channels" and parts[3] == "effects" and parts[4] == "normalize":
                    self._normalize(unquote(parts[2]), body)
                elif len(parts) == 4 and parts[0] == "api" and parts[1] == "channels" and parts[3] == "scte":
                    self._inject_scte(unquote(parts[2]), body)
                elif len(parts) == 4 and parts[0] == "api" and parts[1] == "channels" and parts[3] == "bars":
                    self._bars(unquote(parts[2]), body)
                elif len(parts) == 4 and parts[0] == "api" and parts[1] == "channels" and parts[3] == "detect":
                    self._detect(unquote(parts[2]))
                else:
                    self._json(404, {"ok": False, "detail": "Unknown action"})
            except ServiceError as exc:
                self._json(400, {"ok": False, "detail": str(exc)})
            except ServiceUnavailable as exc:
                self._json(503, {"ok": False, "detail": str(exc)})
            except (ParseError, ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"ok": False, "detail": str(exc)})

        def _body(self, limit: int = 65536) -> bytes:
            length = int(self.headers.get("Content-Length") or "0")
            if length < 0 or length > limit:
                raise ValueError("request body is too large")
            return self.rfile.read(length) if length else b""

        def _save_config(self, body: bytes) -> None:
            raw = json.loads(body.decode("utf-8") or "{}")
            if not isinstance(raw, dict):
                raise ValueError("config must be an object")
            from .channels import normalize_config
            candidate = normalize_config(raw)
            for channel_id, spec in candidate["inputs"].items():
                if spec.get("waiting") == "loop":
                    check_loop_file(spec.get("loop") or "")
            saved = save_config(console.config_path, raw)
            self._json(200, {"ok": True, "config": saved})

        def _bars(self, channel_id: str, body: bytes) -> None:
            if channel_id not in ("A", "B"):
                raise ValueError("channel must be A or B")
            raw = json.loads(body.decode("utf-8") or "{}")
            if not isinstance(raw, dict) or not isinstance(raw.get("enabled"), bool):
                raise ValueError("enabled must be true or false")
            write_flag(bars_path(console.state_dir, channel_id), raw["enabled"])
            self._json(200, {"ok": True, "channel": channel_id, "enabled": raw["enabled"]})

        def _detect(self, channel_id: str) -> None:
            from .detect import detect_channel

            if channel_id not in ("A", "B"):
                raise ValueError("channel must be A or B")
            found = detect_channel(
                channel_id, console.config_path, console.state_dir, probe=console.format_probe,
            )
            self._json(200, found)

        def _save_sideband(self, body: bytes) -> None:
            raw = json.loads(body.decode("utf-8") or "{}")
            if not isinstance(raw, dict):
                raise ValueError("event must be an object")
            added = []
            payload_hex = raw.get("payload_hex")
            if isinstance(payload_hex, str) and payload_hex:
                channel = raw.get("channel")
                if channel not in ("A", "B"):
                    raise ValueError("channel must be A or B")
                for event in events_from_vanc(channel, bytes.fromhex(payload_hex)):
                    added.append(console.sideband.add(event))
            else:
                added.append(console.sideband.add(raw))
            self._json(200, {"ok": True, "events": added})

        def _inject_scte(self, channel_id: str, body: bytes) -> None:
            if channel_id not in ("A", "B"):
                raise ValueError("channel must be A or B")
            if body[:1] == b"{":
                raw = json.loads(body.decode("utf-8"))
                payload = bytes.fromhex(str(raw.get("mom_hex") or ""))
                frame_pts = int(raw.get("pts") or 0)
            else:
                payload = body
                frame_pts = 0
            sections = sections_from_mom(payload, frame_pts)
            port = SPLICE_PORT[channel_id]
            if console.splice_sender is None:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    for section in sections:
                        sock.sendto(section, ("127.0.0.1", port))
                finally:
                    sock.close()
            else:
                for section in sections:
                    console.splice_sender(section, ("127.0.0.1", port))
            console.sideband.add({
                "channel": channel_id,
                "kind": "scte35",
                "label": "SCTE-35",
                "text": f"{len(sections)} section(s) to port {port}",
            })
            self._json(200, {"ok": True, "channel": channel_id, "sections": len(sections), "port": port})

        def _presets(self) -> None:
            presets = [preset_json(item) for item in load_presets(console.state_dir)]
            self._json(200, {"ok": True, "presets": presets, "html_renderer": console.html_backend()})

        def _save_preset(self, body: bytes) -> None:
            raw = json.loads(body.decode("utf-8") or "{}")
            if not isinstance(raw, dict):
                raise ValueError("preset must be an object")
            if raw.get("delete") is True:
                name = raw.get("name")
                if not isinstance(name, str) or not name.strip():
                    raise ValueError("preset name is required")
                delete_preset(console.state_dir, name.strip())
                self._json(200, {"ok": True, "presets": [preset_json(item) for item in load_presets(console.state_dir)]})
                return
            preset = upsert_preset(console.state_dir, raw)
            self._json(200, {"ok": True, "preset": preset_json(preset)})

        def _recall(self, channel_id: str, body: bytes) -> None:
            raw = json.loads(body.decode("utf-8") or "{}")
            if not isinstance(raw, dict):
                raise ValueError("recall body must be an object")
            config = load_config(console.config_path)
            result = recall_effect(console.state_dir, channel_id, raw, config)
            warnings = []
            if result.get("html") and console.html_backend() == "none":
                warnings.append("HTML renderer is not installed; those layers stay transparent")
            if result.get("ndi") and not ndi_available():
                warnings.append("NDI receiver is not installed; those layers stay transparent")
            if warnings:
                result["warning"] = " ".join(warnings)
            self._json(200, {"ok": True, **result})

        def _normalize(self, channel_id: str, body: bytes) -> None:
            raw = json.loads(body.decode("utf-8") or "{}")
            if not isinstance(raw, dict):
                raise ValueError("normalize body must be an object")
            config = load_config(console.config_path)
            result = normalize_channel(console.state_dir, channel_id, raw, config)
            self._json(200, {"ok": True, **result})

        def _still(self, channel_id: str) -> None:
            if channel_id not in ("A", "B"):
                self._json(404, {"ok": False, "detail": "Not found"})
                return
            path = still_path(console.state_dir, channel_id).resolve()
            root = console.state_dir.resolve()
            if path != root and root not in path.parents:
                self._json(404, {"ok": False, "detail": "Not found"})
                return
            if not path.is_file():
                self._json(404, {"ok": False, "detail": "No still yet"})
                return
            data = path.read_bytes()
            if not data.startswith(b"\xff\xd8"):
                self._json(404, {"ok": False, "detail": "Still is not ready"})
                return
            self._bytes(200, "image/jpeg", data)

        def _prom(self, kind: str, query: dict[str, str]) -> None:
            try:
                status, payload = fetch(console.prometheus_url, kind, query)
            except PrometheusError as exc:
                self._json(503, {"ok": False, "detail": exc.detail})
                return
            self._json(status if 200 <= status < 500 else 502, payload)

        def _journal(self, unit: str) -> None:
            try:
                argv = console.journal_builder(normalize_unit(unit), True)
            except ServiceError as exc:
                self._json(400, {"ok": False, "detail": str(exc)})
                return
            except ServiceUnavailable as exc:
                self._sse_error(str(exc))
                return
            try:
                proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            except FileNotFoundError:
                self._sse_error("journalctl is not available on this host")
                return
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    self._sse_line(line.rstrip("\n"))
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                if proc.poll() is None:
                    proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()

        def _sse_error(self, detail: str) -> None:
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self._sse_line(detail, event="error")

        def _sse_line(self, data: str, event: str | None = None) -> None:
            if event:
                self.wfile.write(f"event: {event}\n".encode("utf-8"))
            for line in (data or "").split("\n"):
                self.wfile.write(f"data: {line}\n".encode("utf-8"))
            self.wfile.write(b"\n")
            self.wfile.flush()

        def _json(self, status: int, payload: dict) -> None:
            self._bytes(status, "application/json; charset=utf-8", json.dumps(payload).encode("utf-8"))

        def _bytes(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(console: Console, host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(console))
    httpd.console = console  # type: ignore[attr-defined]
    return httpd


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="NexEncoder operator console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--prometheus", default="http://127.0.0.1:9090")
    parser.add_argument("--config", type=Path, default=repo / "var" / "config.json")
    parser.add_argument("--state", type=Path, default=repo / "var" / "state")
    args = parser.parse_args(argv)
    console = Console(args.prometheus, config_path=args.config, state_dir=args.state)
    httpd = serve(console, args.host, args.port)
    log.info("live feed on http://%s:%s", args.host, args.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
