"""Live feed HTTP: metrics, Prometheus proxy, and service actions."""

import json
import subprocess
import sys
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from nexencoder.console import Console, serve
from nexencoder.services import ServiceUnavailable


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


def test_metrics_feed_does_not_serve_pages():
    httpd, port = _start(Console(prometheus_url="http://127.0.0.1:9"))
    try:
        status, body = _get(port, "/metrics")
        assert status == 200
        assert "nexencoder_up 1" in body
        status, page = _get(port, "/metrics.php")
        assert status == 404 and "Not found" in page
        status, page = _get(port, "/index.php")
        assert status == 404
        status, missing = _get(port, "/../setup.sh")
        assert status == 404
    finally:
        httpd.shutdown()


def test_prometheus_down_is_a_json_error():
    httpd, port = _start(Console(prometheus_url="http://127.0.0.1:9"))
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

    httpd, port = _start(Console(unit_runner=run))
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
        config_path=tmp_path / "config.json",
        state_dir=tmp_path / "state",
        device_lister=lambda: [{"index": 0, "name": "DeckLink Duo (1)"}],
        splice_sender=sender,
    ))
    try:
        body = json.dumps({
            "inputs": {"A": {"format": "720p59.94", "device": "DeckLink Duo (1)"}},
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
        assert status == 200 and "DeckLink Duo (1)" in listing
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

    httpd, port = _start(Console(unit_runner=run, journal_builder=builder))
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


def test_effect_recall_and_normalize_leave_the_pipeline_running(tmp_path):
    from nexencoder.channels import save_config

    config = tmp_path / "config.json"
    state = tmp_path / "state"
    save_config(config, {})
    httpd, port = _start(Console(
        config_path=config, state_dir=state, html_backend=lambda: "none",
    ))
    try:
        status, listed = _get(port, "/api/effects/presets")
        assert status == 200 and json.loads(listed)["html_renderer"] == "none"
        saved = _post(port, "/api/effects/presets", {
            "name": "sidebar",
            "format": "1080i59.94",
            "corners": [[480, 0], [1920, 0], [480, 1080], [1920, 1080]],
            "in_frames": 12,
            "out_frames": 0,
            "layers": [{"level": -1, "source": {"type": "html", "url": "/cg/bug.html"}}],
        })
        assert saved["preset"]["name"] == "sidebar"
        recalled = _post(port, "/api/channels/A/effects/recall", {"preset": "sidebar"})
        assert recalled["frames"] == 12 and "transparent" in recalled["warning"]
        assert (state / "A.effect").read_text(encoding="utf-8").startswith("NXSQ 1")
        cleared = _post(port, "/api/channels/A/effects/normalize", {"frames": 0})
        assert cleared["normalized"] is True and cleared["frames"] == 0
        (state / "A.jpg").write_bytes(b"\xff\xd8\xff\xd9")
        with urlopen(f"http://127.0.0.1:{port}/api/channels/A/still", timeout=3) as response:
            assert response.status == 200
            assert response.read().startswith(b"\xff\xd8")
    finally:
        httpd.shutdown()


def test_auto_saves_the_detected_raster_and_leaves_a_miss_alone(tmp_path):
    calls = []

    def probe(argv):
        calls.append(argv)
        if "DeckLink Duo (2)" in argv:
            return "No input signal detected\n"
        return "Found Decklink mode 1280 x 720 with rate 59.94\n"

    httpd, port = _start(Console(
        config_path=tmp_path / "config.json",
        state_dir=tmp_path / "state",
        format_probe=probe,
    ))
    try:
        found = _post(port, "/api/channels/A/detect", {})
        assert found["ok"] is True and found["format"] == "720p59.94"
        assert found["config"]["inputs"]["A"]["format"] == "720p59.94"
        assert found["config"]["inputs"]["B"]["format"] == "1080i59.94"
        missed = _post(port, "/api/channels/B/detect", {})
        assert missed["ok"] is False and missed["format"] == ""
        assert "No signal" in missed["detail"]
        assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["inputs"]["B"]["format"] == "1080i59.94"
        assert any("decklink" in argv for argv in calls)
    finally:
        httpd.shutdown()


def test_bars_toggle_does_not_rewrite_the_channel_config(tmp_path):
    httpd, port = _start(Console(
        config_path=tmp_path / "config.json",
        state_dir=tmp_path / "state",
    ))
    try:
        turned_on = _post(port, "/api/channels/A/bars", {"enabled": True})
        assert turned_on["enabled"] is True
        status, body = _get(port, "/api/config")
        assert status == 200
        listing = json.loads(body)
        assert listing["bars"]["A"] is True
        assert listing["bars"]["B"] is False
        assert listing["config"]["inputs"]["A"]["source"] == "sdi"
        turned_off = _post(port, "/api/channels/A/bars", {"enabled": False})
        assert turned_off["enabled"] is False
    finally:
        httpd.shutdown()


def _post(port: int, path: str, payload: dict) -> dict:
    request = Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"X-Requested-With": "NexEncoder", "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))
