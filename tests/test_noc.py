"""NOC agent: auth, status contract, host parsers, and channel control."""

import json
import logging
import os
import stat
import subprocess
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from nexencoder.channel import note_fault, note_runs, run_channel
from nexencoder.channels import save_config
from nexencoder.hoststats import (
    HostCollector,
    cpu_percent,
    parse_intel_gpu_top,
    parse_loadavg,
    parse_meminfo,
    parse_netdev,
    parse_nvidia_smi,
    parse_proc_stat,
    read_temperatures,
)
from nexencoder.noc import (
    Agent,
    NocError,
    Systemctl,
    ip_allowed,
    load_noc_config,
    main,
    normalize_outputs,
    parse_proc_udp,
    parse_unix_timestamp,
    redact_url,
    safe_host,
    serve,
    tokens_match,
    udp_peer_connected,
)
from nexencoder.pipeline import whep_url
from nexencoder.srtstats import (
    load_srt_snapshot,
    parse_srt_report,
    redact_log_line,
    redact_secrets,
    start_srt_stats_reader,
    tsp_supports_srt_stats,
)

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-token-0123456789abcdef"


def test_tokens_match_ignores_length_and_rejects_empty():
    assert tokens_match(TOKEN, TOKEN) is True
    assert tokens_match("short", TOKEN) is False
    assert tokens_match(TOKEN + " ", TOKEN) is False
    assert tokens_match("", "") is False
    assert tokens_match(TOKEN, "") is False


def test_ip_allowlist_empty_is_open_and_cidr_matches():
    assert ip_allowed("203.0.113.8", []) is True
    assert ip_allowed("203.0.113.8", ["10.0.0.0/8"]) is False
    assert ip_allowed("10.1.2.3", ["10.0.0.0/8"]) is True
    assert ip_allowed("127.0.0.1", ["127.0.0.1"]) is True
    assert ip_allowed("not-an-ip", ["127.0.0.1"]) is False


def test_redact_url_keeps_host_and_drops_secrets():
    raw = "https://user:s3cret@cdn.example/live/b.m3u8?token=abc&list=1&passphrase=hide"
    cleaned = redact_url(raw)
    assert "s3cret" not in cleaned
    assert "abc" not in cleaned
    assert "hide" not in cleaned
    assert cleaned.startswith("https://user:REDACTED@cdn.example/live/b.m3u8?")
    assert "token=REDACTED" in cleaned
    assert "list=1" in cleaned
    assert redact_url("srt://origin.example:9000") == "srt://origin.example:9000"
    assert redact_url("") == ""


def test_normalize_outputs_rejects_other_units():
    assert normalize_outputs("all") == ("A", "B")
    assert normalize_outputs(["B", "A", "A"]) == ("B", "A")
    with pytest.raises(NocError):
        normalize_outputs(["mediamtx"])
    with pytest.raises(NocError):
        normalize_outputs(["C"])


def test_proc_udp_little_endian_and_listener_match(tmp_path):
    # 203.0.113.9 -> little-endian hex 097100CB, port 9000 is 0x2328, peer port 4000 is 0x0FA0.
    text = "  sl local_address rem_address st\n   0: 00000000:2328 097100CB:0FA0 01\n"
    rows = parse_proc_udp(text, False)
    assert rows == [("0.0.0.0", 9000, "203.0.113.9", 4000, 1)]
    net = tmp_path / "net"
    net.mkdir()
    (net / "udp").write_text(text, encoding="utf-8")
    assert udp_peer_connected(tmp_path, "srt-listener", 9000) is True
    assert udp_peer_connected(tmp_path, "srt-listener", 9101) is False
    (net / "udp").write_text("  sl local_address rem_address st\n   0: 00000000:2328 00000000:0000 07\n", encoding="utf-8")
    assert udp_peer_connected(tmp_path, "srt-listener", 9000) is False
    assert udp_peer_connected(tmp_path / "missing", "srt-listener", 9000) is None


def test_unix_timestamp_scales_and_ignores_blank():
    assert parse_unix_timestamp("@1700000000") == 1700000000
    assert parse_unix_timestamp("1700000000000") == 1700000000
    assert parse_unix_timestamp("n/a") is None
    assert parse_unix_timestamp("") is None


def test_srt_json_and_text_reports_normalize():
    line = (
        '* Info: nexencoder-srt {"send":{"total":{"bytes":100,"packets":9,"retransmit":4,'
        '"lost":3,"dropped":1},"interval":{"bytes":50,"packets":4,"retransmit":1,"lost":0,'
        '"rate-mbps":8.2}},"rtt-ms":12.5}'
    )
    stats = parse_srt_report(line)
    assert stats["rtt_ms"] == 12.5
    assert stats["packets_lost"] == 3
    assert stats["packets_lost_interval"] == 0
    assert stats["retransmits"] == 4
    assert stats["retransmits_interval"] == 1
    assert stats["packets_dropped"] == 1
    assert stats["bandwidth_bps"] == 8_200_000
    text = (
        "Total sent: 1,000 bytes, 9 packets, retransmit: 4 packets, lost: 3 packets, dropped: 1 packets\n"
        "Interval sent: 50 bytes, 4 packets, retransmit: 1 packets, lost: 0 packets\n"
        "RTT: 12.5 ms\n"
    )
    plain = parse_srt_report(text)
    assert plain["rtt_ms"] == 12.5
    assert plain["retransmits"] == 4
    assert plain["bandwidth_bps"] is None


def test_srt_log_line_redacts_a_passphrase():
    line = "caller srt://origin.example:9000?passphrase=supersecret&mode=caller"
    cleaned = redact_log_line(line)
    assert "supersecret" not in cleaned
    assert "passphrase=REDACTED" in cleaned
    push = "ffmpeg -method PUT https://user:supersecret@cdn.example/live/b.m3u8?token=tokensecret"
    cleaned = redact_secrets(push)
    assert "supersecret" not in cleaned
    assert "tokensecret" not in cleaned
    assert "user:REDACTED@" in cleaned
    assert "token=REDACTED" in cleaned


def test_preview_host_rejects_a_smuggled_port_and_brackets_ipv6():
    assert safe_host("enc.example") == "enc.example"
    assert safe_host("2001:db8::10") == "2001:db8::10"
    assert safe_host("[2001:db8::10]") == "2001:db8::10"
    assert safe_host("evil.example:9999") is None
    assert whep_url("A", "2001:db8::10") == "http://[2001:db8::10]:8889/inputA/whep"
    assert whep_url("A", "enc.example") == "http://enc.example:8889/inputA/whep"


def test_srt_snapshot_goes_stale(tmp_path):
    path = tmp_path / "A.srt.json"
    path.write_text(json.dumps({"observed_unix": 1000, "rtt_ms": 4.0, "packets_lost": 1}), encoding="utf-8")
    fresh = load_srt_snapshot(path, now=1010, fresh_seconds=30)
    assert fresh["fresh"] is True and fresh["rtt_ms"] == 4.0
    stale = load_srt_snapshot(path, now=1200, fresh_seconds=30)
    assert stale["fresh"] is False and stale["rtt_ms"] is None and stale["source"] == "stale"


def test_srt_reader_keeps_the_latest_json(tmp_path):
    class _Stream:
        def __init__(self):
            self.lines = [
                b"tsp: listening\n",
                b'nexencoder-srt {"rtt-ms":1,"send":{"total":{"lost":2,"retransmit":3}}}\n',
                b'nexencoder-srt {"rtt-ms":9,"send":{"total":{"lost":4,"retransmit":5}}}\n',
            ]

        def readline(self):
            if not self.lines:
                return b""
            return self.lines.pop(0)

    dest = tmp_path / "A.srt.json"
    start_srt_stats_reader(type("Proc", (), {"stderr": _Stream()})(), dest)
    deadline = time.time() + 2
    raw = {}
    while time.time() < deadline:
        if dest.is_file():
            raw = json.loads(dest.read_text(encoding="utf-8"))
            if raw.get("rtt_ms") == 9:
                break
        time.sleep(0.02)
    assert raw.get("rtt_ms") == 9
    assert raw["packets_lost"] == 4
    assert "observed_unix" in raw


def test_tsp_probe_requires_both_flags():
    def run(argv):
        return subprocess.CompletedProcess(argv, 0, stdout="--statistics-interval\n--json-line\n", stderr="")

    assert tsp_supports_srt_stats("tsp-test", run) is True

    def old(argv):
        return subprocess.CompletedProcess(argv, 0, stdout="--latency\n", stderr="")

    assert tsp_supports_srt_stats("tsp-old", old) is False


def test_channel_enables_srt_stats_and_records_runs(tmp_path):
    calls = []

    class _Proc:
        def __init__(self):
            self.stdout = None

        def poll(self):
            return 0

        def terminate(self):
            return None

    def spawn(argv, **kwargs):
        proc = _Proc()
        if kwargs.get("stdout") is subprocess.PIPE:
            proc.stdout = type("Pipe", (), {"close": lambda self: None})()
        calls.append(argv)
        return proc

    code = run_channel(
        "A", tmp_path / "missing.json", tmp_path / "hls", tmp_path / "state",
        spawn=spawn, cycles=1, install_signals=False, srt_statistics=True,
    )
    assert code == 0
    assert "--json-line=nexencoder-srt" in calls[1]
    assert (tmp_path / "state" / "A.runs").read_text(encoding="utf-8") == "1\n"
    note_fault(tmp_path / "state", "A", "pipeline stalled at frame 10")
    fault = json.loads((tmp_path / "state" / "A.error").read_text(encoding="utf-8"))
    assert fault["message"] == "pipeline stalled at frame 10"
    note_runs(tmp_path / "state", "B", 3)
    assert (tmp_path / "state" / "B.runs").read_text(encoding="utf-8") == "3\n"


def test_host_parsers_and_two_sample_rates(tmp_path):
    idle, total = parse_proc_stat("cpu  100 0 50 850 0 0 0 0\ncpu0 1 0 0 1\n")
    assert cpu_percent(None, (idle, total)) is None
    assert cpu_percent((idle, total), (idle + 10, total + 100)) == 90.0
    memory = parse_meminfo("MemTotal: 1000 kB\nMemAvailable: 250 kB\n")
    assert memory["total_bytes"] == 1000 * 1024
    assert memory["used_percent"] == 75.0
    assert parse_loadavg("0.5 0.25 0.1 1/2 3")["1m"] == 0.5
    net = parse_netdev("eth0: 1000 0 0 0 0 0 0 0 2000 0 0 0 0 0 0 0\nlo: 9 0 0 0 0 0 0 0 9 0 0 0 0 0 0 0\n")
    assert "lo" not in net and net["eth0"] == (1000, 2000)
    gpus = parse_nvidia_smi("Quadro, 12, 34, 1, 512, 8192, 55\n")
    assert gpus[0]["encoder_percent"] == 34
    assert gpus[0]["memory_total_bytes"] == 8192 * 1024 * 1024
    assert parse_nvidia_smi("Bad, N/A, [N/A], [Not Supported], 0, 0, 40")[0]["encoder_percent"] is None
    intel = parse_intel_gpu_top('{"engines":{"Render/3D/0":{"busy":5.5},"Video/0":{"busy":1.25},"VideoEnhance/0":{"busy":2}}}')
    assert intel[0]["utilization_percent"] == 5.5
    assert intel[0]["decoder_percent"] == 1.25
    assert intel[0]["encoder_percent"] == 2
    zone = tmp_path / "class" / "thermal" / "thermal_zone0"
    zone.mkdir(parents=True)
    (zone / "type").write_text("x86_pkg_temp\n", encoding="utf-8")
    (zone / "temp").write_text("48000\n", encoding="utf-8")
    temps = read_temperatures(tmp_path)
    assert temps == [{"name": "x86_pkg_temp", "celsius": 48.0, "source": "thermal"}]

    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    (proc / "stat").write_text("cpu  10 0 10 80 0 0 0 0\n", encoding="utf-8")
    (proc / "meminfo").write_text("MemTotal: 2048 kB\nMemAvailable: 1024 kB\n", encoding="utf-8")
    (proc / "loadavg").write_text("0.1 0.2 0.3 1/1 1\n", encoding="utf-8")
    (proc / "uptime").write_text("100.0 50.0\n", encoding="utf-8")
    (proc / "mounts").write_text(f"/dev/sda1 {tmp_path} ext4 rw 0 0\n", encoding="utf-8")
    (proc / "net" / "dev").write_text(
        "eth0: 1000 0 0 0 0 0 0 0 2000 0 0 0 0 0 0 0\n",
        encoding="utf-8",
    )
    sys_root = tmp_path / "sys"
    sys_root.mkdir()
    clock = {"now": 0.0}
    collector = HostCollector(proc, sys_root, which=lambda _name: None, clock=lambda: clock["now"])
    first = collector.snapshot()
    assert first["cpu_percent"] is None
    assert "cpu_percent_needs_two_samples" in first["limitations"]
    assert first["nics"][0]["rx_bps"] is None
    assert first["gpu"]["available"] is False
    assert first["disks"][0]["mount"] == str(tmp_path)
    (proc / "stat").write_text("cpu  20 0 20 160 0 0 0 0\n", encoding="utf-8")
    (proc / "net" / "dev").write_text(
        "eth0: 6000 0 0 0 0 0 0 0 4000 0 0 0 0 0 0 0\n",
        encoding="utf-8",
    )
    clock["now"] = 1.0
    second = collector.snapshot()
    assert second["cpu_percent"] == 20.0
    assert second["nics"] == [{"name": "eth0", "rx_bps": 40000, "tx_bps": 16000}]
    assert "cpu_percent_needs_two_samples" not in second["limitations"]


def test_systemctl_sudo_is_limited_to_channel_units():
    calls = []

    def run(argv, capture_output, text, timeout, check):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="ActiveState=active\n", stderr="")

    ctl = Systemctl(run=run, use_sudo=True)
    ok, detail = ctl.act("restart", "nexencoder-channel@A.service")
    assert (ok, detail) == (True, "")
    assert calls[0] == ["sudo", "-n", "systemctl", "restart", "nexencoder-channel@A.service"]
    ctl.show("nexencoder-channel@B.service")
    assert calls[1][0] == "systemctl"
    assert "--timestamp=unix" in calls[1]
    with pytest.raises(NocError):
        ctl.act("restart", "mediamtx.service")
    with pytest.raises(NocError):
        ctl.act("enable", "nexencoder-channel@A.service")
    assert len(calls) == 2


def test_privilege_files_do_not_cover_mediamtx_or_the_console():
    rules = (ROOT / "config" / "nexencoder-noc.rules").read_text(encoding="utf-8")
    sudoers = (ROOT / "config" / "sudoers-nexencoder-noc").read_text(encoding="utf-8")
    for text in (rules, sudoers):
        assert "nexencoder-channel@A" in text
        assert "nexencoder-channel@B" in text
        assert "mediamtx" not in text
        assert "nexencoder-console" not in text
        assert "enable" not in text
        assert "disable" not in text
    assert "org.freedesktop.systemd1.manage-units" in rules
    unit = (ROOT / "systemd" / "nexencoder-noc.service").read_text(encoding="utf-8")
    assert "User=nexencoder" in unit
    assert "RestartPreventExitStatus=78" in unit


def _proc(root: Path, udp: str) -> None:
    net = root / "net"
    net.mkdir(parents=True)
    (root / "stat").write_text("cpu  10 0 10 80 0 0 0 0\n", encoding="utf-8")
    (root / "meminfo").write_text("MemTotal: 4096 kB\nMemAvailable: 2048 kB\n", encoding="utf-8")
    (root / "loadavg").write_text("0.2 0.2 0.2 1/1 1\n", encoding="utf-8")
    (root / "uptime").write_text("500.0 10\n", encoding="utf-8")
    (root / "mounts").write_text("", encoding="utf-8")
    (net / "dev").write_text("eth0: 100 0 0 0 0 0 0 0 100 0 0 0 0 0 0 0\n", encoding="utf-8")
    (net / "udp").write_text(udp, encoding="utf-8")


class _Ctl:
    def __init__(self):
        self.calls = []
        self.fail = set()
        self.text = {
            "nexencoder-channel@A.service": (
                "ActiveState=active\nSubState=running\nResult=success\nNRestarts=2\n"
                "ActiveEnterTimestamp=@1700000000\n"
            ),
            "nexencoder-channel@B.service": (
                "ActiveState=inactive\nSubState=dead\nResult=success\nNRestarts=0\n"
                "ActiveEnterTimestamp=\n"
            ),
        }

    def show(self, unit):
        self.calls.append(("show", unit))
        if unit not in self.text:
            raise NocError(400, "bad_request", "unit is not a channel output")
        return self.text[unit]

    def act(self, action, unit):
        self.calls.append((action, unit))
        if unit not in self.text or action not in {"start", "stop", "restart"}:
            raise NocError(400, "bad_request", "action is not allowed")
        if unit in self.fail:
            return False, "access denied"
        self.text[unit] = (
            "ActiveState=active\nSubState=running\nResult=success\nNRestarts=3\n"
            "ActiveEnterTimestamp=@1700000100\n"
        )
        return True, ""


def _agent(tmp_path: Path, ctl: _Ctl | None = None) -> Agent:
    proc = tmp_path / "proc"
    _proc(proc, "  sl local_address rem_address st\n   0: 00000000:2328 097100CB:0FA0 01\n")
    config = tmp_path / "encoder.json"
    save_config(config, {
        "outputs": {
            "A": {"mode": "srt-listener", "bitrate": 8, "host": "", "port": 9000, "url": ""},
            "B": {
                "mode": "hls-push",
                "bitrate": 10,
                "host": "",
                "port": 9101,
                "url": "https://user:s3cret@cdn.example/live/b.m3u8?token=abc&list=1",
            },
        }
    })
    state = tmp_path / "state"
    state.mkdir()
    now = time.time()
    progress = state / "A.progress"
    progress.write_text(
        "frame=40\nfps=29.97\nbitrate=8100.5kbits/s\ndrop_frames=2\ndup_frames=1\nprogress=continue\n",
        encoding="utf-8",
    )
    os.utime(progress, (now, now))
    (state / "A.runs").write_text("2\n", encoding="utf-8")
    (state / "A.srt.json").write_text(json.dumps({
        "observed_unix": now,
        "rtt_ms": 12.5,
        "packets_lost": 3,
        "packets_lost_interval": 0,
        "retransmits": 4,
        "retransmits_interval": 1,
        "packets_dropped": 1,
        "bandwidth_bps": 8200000,
    }), encoding="utf-8")
    (state / "A.error").write_text(json.dumps({"message": "pipeline stalled at frame 9", "unix": now - 5}), encoding="utf-8")
    noc = {
        "encoder_id": "enc-studio-1",
        "site": "Studio A",
        "location": "Rack 4",
        "preview_host": "enc.example",
        "preview_port": 8889,
        "preview_scheme": "http",
        "allow": [],
        "trust_proxy": False,
        "use_sudo": False,
    }
    host = HostCollector(proc, tmp_path / "sys", which=lambda _name: None)
    (tmp_path / "sys").mkdir()
    return Agent(
        TOKEN, noc, config, state, tmp_path / "hls",
        host=host, systemctl=ctl or _Ctl(), proc_root=proc, hostname="enc-host",
    )


def test_status_contract_and_health_are_separate(tmp_path):
    ctl = _Ctl()
    agent = _agent(tmp_path, ctl)
    calls_before = len(ctl.calls)
    health = agent.health()
    assert ctl.calls[calls_before:] == []
    assert health["ok"] is True
    assert health["encoder_id"] == "enc-studio-1"
    assert health["version"]
    assert set(health) == {"ok", "api_version", "encoder_id", "hostname", "version", "uptime_seconds"}
    assert "outputs" not in health

    body = agent.status("enc.example")
    assert body["ok"] is True
    assert body["api_version"] == "v1"
    assert body["identity"]["site"] == "Studio A"
    assert body["identity"]["location"] == "Rack 4"
    assert body["identity"]["model"] == "NexEncoder"
    assert body["identity"]["uptime_seconds"] == 500
    assert [item[1] for item in ctl.calls if item[0] == "show"] == [
        "nexencoder-channel@A.service",
        "nexencoder-channel@B.service",
    ]
    outputs = {item["id"]: item for item in body["outputs"]}
    channel_a = outputs["A"]
    assert channel_a["encoding"] is True
    assert channel_a["connected"] is True
    assert channel_a["connected_evidence"] == "srt-stats"
    assert channel_a["video"]["bitrate_configured_bps"] == 8_000_000
    assert channel_a["video"]["bitrate_measured_bps"] == 8_100_500
    assert channel_a["video"]["fps_measured"] == 29.97
    assert channel_a["video"]["dropped_frames"] == 2
    assert channel_a["video"]["dup_frames"] == 1
    assert channel_a["video"]["codec"] == "h264"
    assert channel_a["audio"]["levels_available"] is False
    assert channel_a["audio"]["levels_dbfs"] is None
    assert channel_a["pipeline"]["encode"] == "ffmpeg-libx264"
    assert channel_a["pipeline"]["egress"] == "tsduck-srt"
    assert channel_a["pipeline"]["gstreamer"] is False
    assert channel_a["preview"]["whep"] == "http://enc.example:8889/inputA/whep"
    assert channel_a["transport"]["mode"] == "listener"
    assert [item["id"] for item in channel_a["destinations"]] == ["1"]
    assert channel_a["destinations"][0]["destination"] == channel_a["transport"]["destination"]
    assert channel_a["destinations"][0]["connected"] is True
    assert channel_a["transport"]["latency_ms"] == 1500
    assert channel_a["transport"]["srt"]["rtt_ms"] == 12.5
    assert channel_a["transport"]["srt"]["bandwidth_bps"] == 8200000
    assert channel_a["service_restarts"] == 2
    assert channel_a["pipeline_runs"] == 2
    assert channel_a["last_error"] == "pipeline stalled at frame 9"
    assert channel_a["uptime_seconds"] > 0
    channel_b = outputs["B"]
    assert channel_b["encoding"] is False
    assert channel_b["connected"] is False
    assert channel_b["pipeline"]["egress"] == "ffmpeg-hls"
    assert "s3cret" not in channel_b["transport"]["destination"]
    assert "abc" not in json.dumps(body)
    assert "token=REDACTED" in channel_b["transport"]["destination"]
    assert "mediamtx.service" not in json.dumps(body)
    assert "audio_levels_unavailable" in body["limitations"]
    assert "hls_push_connected_is_process" in body["limitations"]
    assert "srt_stats_unavailable" not in body["limitations"]


def test_status_lists_every_destination(tmp_path):
    agent = _agent(tmp_path)
    save_config(agent.encoder_config, {
        "inputs": {"A": {"bitrate": 12}},
        "outputs": {"A": [
            {"mode": "srt-listener", "port": 9000},
            {"id": "2", "mode": "hls-pull", "url": ""},
        ]},
    })
    body = agent.status("enc.example")
    channel_a = {item["id"]: item for item in body["outputs"]}["A"]
    assert channel_a["video"]["bitrate_configured_bps"] == 12_000_000
    assert channel_a["connected"] is True
    assert channel_a["transport"]["config_mode"] == "srt-listener"
    assert [item["id"] for item in channel_a["destinations"]] == ["1", "2"]
    assert channel_a["destinations"][1]["connected"] is False
    assert channel_a["destinations"][1]["destination"].endswith(f"{os.sep}A{os.sep}2{os.sep}index.m3u8")
    assert channel_a["pipeline"]["egress"] == "mixed"


def test_control_logs_caller_and_returns_state(tmp_path, caplog):
    ctl = _Ctl()
    agent = _agent(tmp_path, ctl)
    with caplog.at_level(logging.INFO, logger="nexencoder.noc"):
        body = agent.control("restart", ["A"], "203.0.113.8")
    assert body["ok"] is True
    assert body["caller"] == "203.0.113.8"
    assert body["outputs"][0]["active_state"] == "active"
    assert body["outputs"][0]["service_restarts"] == 3
    assert ("restart", "nexencoder-channel@A.service") in ctl.calls
    assert "203.0.113.8" in caplog.text
    assert "restart" in caplog.text
    audit = (tmp_path / "state" / "audit.log").read_text(encoding="utf-8")
    assert "noc.control" in audit
    assert "203.0.113.8" in audit
    assert '"action":"restart"' in audit
    ctl.fail.add("nexencoder-channel@B.service")
    both = agent.control("restart", "all", "203.0.113.8")
    assert both["ok"] is False
    assert [item["id"] for item in both["outputs"]] == ["A", "B"]
    assert both["outputs"][1]["detail"] == "access denied"
    with pytest.raises(NocError):
        agent.control("restart", ["mediamtx"], "203.0.113.8")
    assert not any("mediamtx" in call[1] for call in ctl.calls if len(call) == 2)


def _start(agent: Agent):
    httpd = serve(agent, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1]


def _request(port: int, path: str, token: str | None = None, method: str = "GET", body: dict | None = None, headers: dict | None = None):
    headers = dict(headers or {})
    data = None
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_http_requires_token_and_allowlist(tmp_path):
    ctl = _Ctl()
    agent = _agent(tmp_path, ctl)
    httpd, port = _start(agent)
    try:
        status, body = _request(port, "/api/noc/v1/health")
        assert status == 401 and json.loads(body)["detail"] == "missing_token"
        status, body = _request(port, "/api/noc/v1/health", token="wrong-token-0123456789abcd")
        assert status == 401 and json.loads(body)["detail"] == "bad_token"
        status, body = _request(port, f"/api/noc/v1/health?token={TOKEN}", token=TOKEN)
        assert status == 400
        status, body = _request(port, "/api/noc/v1/health", token=TOKEN)
        assert status == 200 and json.loads(body)["encoder_id"] == "enc-studio-1"
        status, body = _request(port, "/api/noc/v1/status", token=TOKEN)
        assert status == 200
        payload = json.loads(body)
        assert payload["outputs"][0]["preview"]["whep"].endswith("/inputA/whep")
        status, _body = _request(port, "/api/noc/v1/outputs/mediamtx/restart", token=TOKEN, method="POST")
        assert status == 400
        status, body = _request(port, "/api/noc/v1/outputs/A/restart", token=TOKEN, method="POST")
        assert status == 200 and json.loads(body)["outputs"][0]["ok"] is True
        status, body = _request(port, "/api/noc/v1/control", token=TOKEN, method="POST", body={"action": "stop", "outputs": ["B"]})
        assert status == 200
        assert json.loads(body)["outputs"][0]["id"] == "B"
        ctl.fail.add("nexencoder-channel@A.service")
        ctl.fail.add("nexencoder-channel@B.service")
        status, body = _request(port, "/api/noc/v1/outputs/restart", token=TOKEN, method="POST")
        assert status == 502
        assert json.loads(body)["ok"] is False
        status, _body = _request(port, "/metrics", token=TOKEN)
        assert status == 404
        agent.allow = ["10.0.0.0/8"]
        status, body = _request(port, "/api/noc/v1/health", token=TOKEN)
        assert status == 403 and json.loads(body)["error"] == "forbidden"
        agent.allow = ["127.0.0.1"]
        status, _body = _request(port, "/api/noc/v1/health", token=TOKEN)
        assert status == 200
        agent.trust_proxy = True
        agent.allow = ["203.0.113.50"]
        status, _body = _request(
            port, "/api/noc/v1/health", token=TOKEN,
            headers={"X-Forwarded-For": "203.0.113.50, 127.0.0.1"},
        )
        assert status == 200
        status, body = _request(
            port, "/api/noc/v1/health", token=TOKEN,
            headers={"X-Forwarded-For": "198.51.100.8"},
        )
        assert status == 403
        agent.trust_proxy = False
        status, body = _request(
            port, "/api/noc/v1/health", token=TOKEN,
            headers={"X-Forwarded-For": "203.0.113.50"},
        )
        assert status == 403
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_main_refuses_a_missing_or_short_token(tmp_path):
    assert main(["--config", str(tmp_path / "noc.json"), "--token-file", str(tmp_path / "missing")]) == 78
    token = tmp_path / "short"
    token.write_text("too-short\n", encoding="utf-8")
    assert main(["--config", str(tmp_path / "noc.json"), "--token-file", str(token)]) == 78


def test_effect_recall_clear_and_status(tmp_path):
    from nexencoder.effects import delete_preset, upsert_preset

    agent = _agent(tmp_path)
    agent.html_backend = lambda: "none"
    state = tmp_path / "state"
    upsert_preset(state, {
        "name": "Bug",
        "format": "1080i59.94",
        "in_frames": 0,
        "out_frames": 12,
        "layers": [],
    })
    upsert_preset(state, {
        "name": "Lower",
        "format": "1080i59.94",
        "in_frames": 4,
        "out_frames": 4,
        "layers": [{"level": 1, "source": {"type": "html", "url": "https://cg.example/bug"}}],
    })
    httpd, port = _start(agent)
    try:
        status, body = _request(port, "/api/noc/v1/effects/Bug")
        assert status == 401
        status, body = _request(port, "/api/noc/v1/effects/Bug", token=TOKEN)
        view = json.loads(body)
        assert status == 200 and view["exists"] is True and view["valid"] is True and view["active"] == []
        status, body = _request(
            port, "/api/noc/v1/outputs/A/effects/recall", token=TOKEN, method="POST", body={"preset": "Bug"},
        )
        recalled = json.loads(body)
        assert status == 200 and recalled["channel"] == "A" and recalled["preset"] == "Bug"
        assert recalled["active"] == ["A"] and recalled["frames"] == 0
        status, body = _request(port, "/api/noc/v1/effects/Bug", token=TOKEN)
        assert json.loads(body)["active"] == ["A"]
        status, body = _request(port, "/api/noc/v1/outputs/A/effects/clear", token=TOKEN, method="POST")
        cleared = json.loads(body)
        assert status == 200 and cleared["normalized"] is True and cleared["frames"] == 12 and cleared["active"] == []
        status, body = _request(port, "/api/noc/v1/outputs/A/effects/recall", token=TOKEN, method="POST", body={"preset": "Missing"})
        assert status == 400 and "unknown preset" in json.loads(body)["detail"]
        status, body = _request(port, "/api/noc/v1/outputs/C/effects/recall", token=TOKEN, method="POST", body={"preset": "Bug"})
        assert status == 400 and json.loads(body)["detail"] == "output must be A or B"
        status, body = _request(port, "/api/noc/v1/effects/Lower", token=TOKEN)
        assert "HTML renderer" in json.loads(body)["warning"]
        status, body = _request(
            port, "/api/noc/v1/outputs/B/effects/recall", token=TOKEN, method="POST", body={"preset": "Bug"},
        )
        assert status == 200 and json.loads(body)["active"] == ["B"]
        delete_preset(state, "Bug")
        status, body = _request(port, "/api/noc/v1/effects/Bug", token=TOKEN)
        gone = json.loads(body)
        assert gone["exists"] is False and gone["valid"] is False and gone["active"] == ["B"]
        status, body = _request(port, "/api/noc/v1/effects", token=TOKEN)
        listed = {item["name"]: item for item in json.loads(body)["effects"]}
        assert listed["Bug"]["exists"] is False and listed["Lower"]["exists"] is True
        status, _body = _request(port, "/api/noc/v1/effects/Bug", token=TOKEN, method="POST")
        assert status == 405
    finally:
        httpd.shutdown()
    audit = (state / "audit.log").read_text(encoding="utf-8")
    assert "noc.effect.recall" in audit and "noc.effect.clear" in audit


def test_load_noc_config_drops_bad_allow_entries(tmp_path):
    path = tmp_path / "noc.json"
    path.write_text(json.dumps({
        "site": "Studio\nA",
        "allow": ["10.0.0.0/8", "not-a-cidr", "203.0.113.5"],
        "listen": "nope",
        "port": 8790,
    }), encoding="utf-8")
    loaded = load_noc_config(path)
    assert loaded["site"] == "Studio A"
    assert loaded["allow"] == ["10.0.0.0/8", "203.0.113.5"]
    assert loaded["listen"] == "0.0.0.0"
    assert loaded["token_file"] == "/etc/nexencoder/noc.token"


def test_token_mode_warning(tmp_path):
    from nexencoder.noc import token_world_readable

    path = tmp_path / "noc.token"
    path.write_text(TOKEN + "\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    assert token_world_readable(path) is None
    path.chmod(0o644)
    assert token_world_readable(path)


def test_effect_slug_recall_and_on_air(tmp_path):
    from nexencoder.effects import upsert_preset
    from nexencoder.noc import effect_slug

    assert effect_slug("Lower Third") == "lower_third"
    assert effect_slug("Side-bar") == "side-bar"
    assert effect_slug("A/B bug") == "a_b_bug"
    assert effect_slug("  Bug!! ") == "bug"
    agent = _agent(tmp_path)
    state = tmp_path / "state"
    upsert_preset(state, {
        "name": "Lower Third",
        "format": "1080i59.94",
        "in_frames": 0,
        "out_frames": 8,
        "layers": [],
    })
    httpd, port = _start(agent)
    try:
        status, body = _request(port, "/api/noc/v1/outputs/a/effects/lower_third", token=TOKEN)
        quiet = json.loads(body)
        assert status == 200 and quiet["exists"] is True and quiet["active"] is False and quiet["slug"] == "lower_third"
        status, body = _request(port, "/api/noc/v1/outputs/a/effects/recall/lower_third", token=TOKEN, method="POST")
        recalled = json.loads(body)
        assert status == 200 and recalled["preset"] == "Lower Third" and recalled["channel"] == "A"
        status, body = _request(port, "/api/noc/v1/outputs/a/effects/lower_third", token=TOKEN)
        assert json.loads(body)["active"] is True
        status, body = _request(port, "/api/noc/v1/outputs/b/effects/lower_third", token=TOKEN)
        assert json.loads(body)["active"] is False
        status, body = _request(port, "/api/noc/v1/effects/lower_third", token=TOKEN)
        assert json.loads(body)["name"] == "Lower Third" and json.loads(body)["active"] == ["A"]
        status, body = _request(port, "/api/noc/v1/outputs/a/effects/clear", token=TOKEN, method="POST")
        assert status == 200 and json.loads(body)["active"] == []
        status, body = _request(port, "/api/noc/v1/outputs/a/effects/recall/missing_store", token=TOKEN, method="POST")
        assert status == 400 and "unknown preset" in json.loads(body)["detail"]
        status, body = _request(port, "/api/noc/v1/outputs/a/effects/missing_store", token=TOKEN)
        missing = json.loads(body)
        assert status == 200 and missing["exists"] is False and missing["active"] is False
        status, body = _request(port, "/api/noc/v1/outputs/a/restart", token=TOKEN, method="POST")
        assert status == 200
        upsert_preset(state, {
            "name": "Bug",
            "format": "1080i59.94",
            "in_frames": 0,
            "out_frames": 0,
            "layers": [],
        })
        upsert_preset(state, {
            "name": "bug",
            "format": "1080i59.94",
            "in_frames": 0,
            "out_frames": 0,
            "layers": [],
        })
        status, body = _request(port, "/api/noc/v1/outputs/a/effects/recall/bug", token=TOKEN, method="POST")
        assert status == 400 and json.loads(body)["detail"] == "preset slug is not unique"
        status, body = _request(port, "/api/noc/v1/outputs/a/effects/bug", token=TOKEN)
        assert status == 400 and json.loads(body)["detail"] == "preset slug is not unique"
    finally:
        httpd.shutdown()


def test_token_file_is_reread_and_a_revoked_token_fails(tmp_path):
    from nexencoder.noc import create_api_token, delete_api_token, list_api_tokens, revoke_api_token

    path = tmp_path / "noc.token"
    agent = _agent(tmp_path)
    agent.token_path = path
    assert agent.token_matches(TOKEN) is True
    path.write_text(TOKEN + "\n", encoding="utf-8")
    assert agent.token_matches(TOKEN) is True
    created = create_api_token(path, "Companion")
    assert agent.token_matches(created["secret"]) is True
    assert agent.token_matches(TOKEN) is True
    public = json.dumps(list_api_tokens(path))
    assert TOKEN not in public and created["secret"] not in public and "secret" not in public
    revoke_api_token(path, "legacy")
    assert agent.token_matches(TOKEN) is False
    assert agent.token_matches(created["secret"]) is True
    delete_api_token(path, created["id"])
    assert agent.token_matches(created["secret"]) is False
    assert agent.token_matches(TOKEN) is False
