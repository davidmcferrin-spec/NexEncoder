"""Console HTTP: metrics, static files, Prometheus proxy, and service actions."""

import json
import subprocess
import sys
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from nexencoder.console import Console, serve
from nexencoder.services import ServiceUnavailable

ROOT_WEB = __import__("pathlib").Path(__file__).resolve().parents[1] / "web"


def _start(console: Console):
    httpd = serve(console, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1]


def _get(port: int, path: str) -> tuple[int, str]:
    try:
        with urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_metrics_and_static_pages():
    httpd, port = _start(Console(ROOT_WEB, prometheus_url="http://127.0.0.1:9"))
    try:
        status, body = _get(port, "/metrics")
        assert status == 200
        assert "nexencoder_up 1" in body
        status, html = _get(port, "/metrics.html")
        assert status == 200 and "Metrics" in html
        status, html = _get(port, "/services.html")
        assert status == 200 and "Services" in html
        status, missing = _get(port, "/../setup.sh")
        assert status == 404
    finally:
        httpd.shutdown()


def test_prometheus_down_is_a_json_error():
    httpd, port = _start(Console(ROOT_WEB, prometheus_url="http://127.0.0.1:9"))
    try:
        status, body = _get(port, "/api/prom/query?query=up")
        assert status == 503
        payload = json.loads(body)
        assert "Prometheus" in payload["detail"]
    finally:
        httpd.shutdown()


def test_service_action_requires_header_and_allowlist():
    calls = []

    def run(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    httpd, port = _start(Console(ROOT_WEB, unit_runner=run))
    try:
        request = Request(f"http://127.0.0.1:{port}/api/services/prometheus.service?action=restart", method="POST")
        try:
            urlopen(request, timeout=3)
            status = 200
            detail = ""
        except HTTPError as exc:
            status = exc.code
            detail = exc.read().decode("utf-8")
        assert status == 403
        assert calls == []

        request = Request(
            f"http://127.0.0.1:{port}/api/services/nginx?action=restart",
            method="POST",
            headers={"X-Requested-With": "NexEncoder"},
        )
        try:
            urlopen(request, timeout=3)
            raise AssertionError("expected rejection")
        except HTTPError as exc:
            assert exc.code == 400

        request = Request(
            f"http://127.0.0.1:{port}/api/services/prometheus.service?action=restart",
            method="POST",
            headers={"X-Requested-With": "NexEncoder"},
        )
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["ok"] is True
        assert calls == [["restart", "prometheus.service"]]
    finally:
        httpd.shutdown()


def test_config_preview_and_scte_injection(tmp_path):
    sent = []

    def sender(payload, addr):
        sent.append((payload, addr))

    httpd, port = _start(Console(
        ROOT_WEB,
        config_path=tmp_path / "config.json",
        state_dir=tmp_path / "state",
        device_lister=lambda: [{"index": 0, "name": "DeckLink Duo 2 (1)"}],
        splice_sender=sender,
    ))
    try:
        body = json.dumps({
            "inputs": {"A": {"format": "720p59.94", "device": "DeckLink Duo 2 (1)"}},
            "outputs": {"A": {"mode": "srt-listener", "bitrate": 12, "host": "", "port": 9000, "url": ""}},
        }).encode("utf-8")
        request = Request(
            f"http://127.0.0.1:{port}/api/config",
            data=body,
            method="POST",
            headers={"X-Requested-With": "NexEncoder", "Content-Type": "application/json"},
        )
        with urlopen(request, timeout=3) as response:
            saved = json.loads(response.read().decode("utf-8"))
        assert saved["config"]["outputs"]["A"]["bitrate"] == 12
        status, listing = _get(port, "/api/devices")
        assert status == 200 and "DeckLink Duo 2 (1)" in listing
        status, preview = _get(port, "/api/preview")
        assert status == 200 and "/inputA/whep" in preview
        from nexencoder import scte104 as s104
        mom = s104.build_mom(s104.Mom(0, 0, 1, 0, 0, s104.TIME_NONE, b"", (s104.SpliceNull104(),)))
        request = Request(
            f"http://127.0.0.1:{port}/api/channels/A/scte",
            data=mom,
            method="POST",
            headers={"X-Requested-With": "NexEncoder", "Content-Type": "application/octet-stream"},
        )
        with urlopen(request, timeout=3) as response:
            injected = json.loads(response.read().decode("utf-8"))
        assert injected["port"] == 19101 and injected["sections"] == 1
        assert sent[0][1] == ("127.0.0.1", 19101)
        status, metrics = _get(port, "/metrics")
        assert status == 200
        assert 'format="720p59.94"' in metrics
        assert "12000000" in metrics
        assert 'nexencoder_input_scte104_messages_total{input="A"} 1' in metrics
        status, sideband = _get(port, "/api/sideband")
        assert status == 200 and "scte35" in sideband
    finally:
        httpd.shutdown()


def test_journal_stream_and_missing_systemctl():
    def builder(unit, follow):
        assert unit == "prometheus.service" and follow is True
        return [sys.executable, "-c", "print('hello-journal')"]

    def run(args):
        raise ServiceUnavailable("systemctl is not available on this host")

    httpd, port = _start(Console(ROOT_WEB, unit_runner=run, journal_builder=builder))
    try:
        status, body = _get(port, "/api/services")
        assert status == 200
        assert json.loads(body)["systemctl"] is False
        status, stream = _get(port, "/api/services/prometheus.service/log")
        assert status == 200
        assert "hello-journal" in stream
        status, rejected = _get(port, "/api/services/nginx/log")
        assert status == 400
    finally:
        httpd.shutdown()
