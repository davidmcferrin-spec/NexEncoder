"""Capture, splice, and delivery argv. Nothing here opens a DeckLink device."""

import os
import subprocess
import threading

import pytest

from nexencoder import scte104 as s104
from nexencoder.channel import EXIT_CONFIG, open_progress_pipe, progress_target, run_channel, run_preview
from nexencoder.channels import save_config
from nexencoder.pipeline import (
    SRT_STATS_INTERVAL_MS,
    SRT_STATS_PREFIX,
    capture_argv,
    clock_argv,
    egress_argv,
    preview_argv,
    sections_from_mom,
    splice_argv,
)
from nexencoder.scte35 import SpliceNull, parse_section
from nexencoder.signal import h264_level, signal_mode


def test_capture_keeps_vanc_and_leaves_the_monitor_off_this_process():
    argv = capture_argv("A", "DeckLink Duo (1)", "1080i59.94", 8, progress_path="/run/A.progress")
    assert argv[0] == "ffmpeg"
    assert argv[argv.index("-raw_format") + 1] == "yuv422p10"
    assert argv[argv.index("-format_code") + 1] == "Hi59"
    assert argv[argv.index("-i") + 1] == "DeckLink Duo (1)"
    assert argv[argv.index("-a53cc") + 1] == "1"
    assert "pipe:1" in argv
    assert "8554" not in " ".join(argv)
    assert "libopus" not in argv
    assert "-progress" in argv
    assert "+ildct+ilme" in argv
    graph = argv[argv.index("-filter_complex") + 1]
    assert graph.index("[0:v]split=2[vsrc][vstill]") < graph.index("nexeffect")
    assert graph.index("nexeffect") < graph.index("[vprog]")
    assert "[vprev]" not in graph
    assert "interlaced=1" in graph
    assert "nextone=hold=" in graph
    assert argv[argv.index("-signal_loss_action") + 1] == "repeat"
    assert argv[argv.index("-a53cc") + 1] == "1"


def test_progressive_capture_skips_interlace_flags():
    argv = capture_argv("B", "DeckLink Duo (2)", "720p59.94", 12)
    assert argv[argv.index("-format_code") + 1] == "hp59"
    assert "+ildct+ilme" not in argv
    assert "8554" not in " ".join(argv)
    graph = argv[argv.index("-filter_complex") + 1]
    assert "interlaced=0" in graph
    assert h264_level(signal_mode("1080i59.94")) == "4.1"
    assert h264_level(signal_mode("1080p59.94")) == "4.2"
    assert signal_mode("1080p59.94").format_code == "Hp59"
    assert signal_mode("1080p60").format_code == "Hp60"
    assert h264_level(signal_mode("1080p60")) == "4.2"
    assert signal_mode("1080p50").format_code == "Hp50"


def test_srt_input_keeps_an_encode_clock_without_a_still():
    argv = clock_argv("A", "1080p59.94", 8, hold_path="/var/lib/nexencoder/state/A.wait", pcm_path="/var/lib/nexencoder/state/cg/A-live.pcm")
    graph = argv[argv.index("-filter_complex") + 1]
    assert argv[argv.index("-f") + 1] == "lavfi"
    assert "color=c=black:s=1920x1080:r=60000/1001" in argv
    assert any(item.startswith("anullsrc=") for item in argv)
    assert "nexeffect=" in graph and "nextone=" in graph
    assert "[vstill]" not in graph and "[vprev]" not in graph
    assert "8554" not in " ".join(argv)
    assert "image2" not in argv


def test_preview_decodes_the_program_feed():
    argv = preview_argv("A")
    assert argv[argv.index("-i") + 1].startswith("udp://239.255.1.1:19001?localaddr=127.0.0.1")
    assert "nexeffect" not in " ".join(argv)
    assert "decklink" not in argv
    assert argv[argv.index("-f") + 1] == "mpegts"
    assert argv[-1] == "rtsp://127.0.0.1:8554/inputA"
    assert "libopus" in argv
    assert "yadif=" in argv[argv.index("-vf") + 1]
    other = preview_argv("B")
    assert other[other.index("-i") + 1].startswith("udp://239.255.1.1:19002?localaddr=127.0.0.1")
    assert other[-1] == "rtsp://127.0.0.1:8554/inputB"


def test_preview_exit_restarts_only_the_monitor():
    calls = []
    stop = threading.Event()

    def spawn(argv, **_kwargs):
        calls.append(argv)
        return _Proc()

    def pause(_seconds):
        if len(calls) >= 2:
            stop.set()

    run_preview("A", stop, spawn, pause)
    assert len(calls) == 2
    assert all(item[-1] == "rtsp://127.0.0.1:8554/inputA" for item in calls)
    assert all("pipe:1" not in item for item in calls)


def test_splice_and_srt_listener():
    splice = splice_argv("A")
    assert splice[splice.index("-O") + 1] == "ip"
    assert "239.255.1.1:19001" in splice
    assert splice[splice.index("--local-address") + 1] == "127.0.0.1"
    assert "127.0.0.1:19101" in splice
    assert "496/0x86" in splice
    listener = egress_argv("A", "srt-listener", "", 9000, "", "/tmp/hls")
    assert listener[listener.index("-I") + 1] == "ip"
    assert listener[listener.index("--local-address") + 1] == "127.0.0.1"
    assert listener[listener.index("--listener") + 1] == ":9000"
    assert listener[listener.index("--statistics-interval") + 1] == str(SRT_STATS_INTERVAL_MS)
    assert f"--json-line={SRT_STATS_PREFIX}" in listener
    quiet = egress_argv("A", "srt-listener", "", 9000, "", "/tmp/hls", srt_statistics=False)
    assert "--json-line" not in " ".join(quiet)
    caller = egress_argv("B", "srt-caller", "origin.example", 9001, "", "/tmp/hls")
    assert caller[caller.index("--caller") + 1] == "origin.example:9001"
    with pytest.raises(ValueError):
        egress_argv("A", "srt-caller", "", 9000, "", "/tmp/hls")


def test_hls_pull_copies_and_push_puts():
    pull = egress_argv("A", "hls-pull", "", 0, "", "/var/lib/nexencoder/hls/A")
    assert any(item.startswith("udp://239.255.1.1:19001?localaddr=127.0.0.1") for item in pull)
    assert pull[pull.index("-c") + 1] == "copy"
    assert pull[-1].endswith("index.m3u8")
    push = egress_argv("B", "hls-push", "", 0, "https://origin.example/live/b.m3u8", "/tmp")
    assert push[push.index("-method") + 1] == "PUT"
    with pytest.raises(ValueError):
        egress_argv("A", "hls-push", "", 0, "", "/tmp")


def test_sections_from_mom_are_scte35():
    mom = s104.build_mom(s104.Mom(0, 0, 1, 0, 0, s104.TIME_NONE, b"", (s104.SpliceNull104(),)))
    sections = sections_from_mom(mom)
    assert len(sections) == 1
    assert isinstance(parse_section(sections[0]).command, SpliceNull)


class _Proc:
    def __init__(self):
        self.stdout = None
        self.terminated = False

    def poll(self):
        return 0

    def terminate(self):
        self.terminated = True


def test_run_channel_starts_capture_splice_and_egress(tmp_path):
    calls = []
    pipes = []

    class _Pipe:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    def spawn(argv, **kwargs):
        proc = _Proc()
        if kwargs.get("stdout") is subprocess.PIPE:
            proc.stdout = _Pipe()
            pipes.append(proc.stdout)
        calls.append((argv, kwargs))
        return proc

    code = run_channel(
        "A", tmp_path / "missing.json", tmp_path / "hls", tmp_path / "state",
        spawn=spawn, cycles=1, install_signals=False,
    )
    assert code == 0
    assert [item[0][0] for item in calls] == ["ffmpeg", "tsp", "tsp"]
    assert calls[0][1]["stdout"] is subprocess.PIPE
    assert calls[2][1]["stdin"] is pipes[0]
    assert "8554" not in " ".join(calls[0][0])
    assert pipes[0].closed is True
    assert "--listener" in calls[1][0]
    assert str(tmp_path / "state" / "A.progress") in calls[0][0]


def test_run_channel_refuses_a_caller_with_no_address(tmp_path):
    config = {
        "inputs": {"A": {"format": "1080i59.94", "device": "DeckLink Duo (1)"}},
        "outputs": {"A": {"mode": "srt-caller", "bitrate": 8, "host": "", "port": 9000, "url": ""}},
    }
    path = tmp_path / "config.json"
    save_config(path, config)
    assert run_channel("A", path, tmp_path / "hls", cycles=1, install_signals=False) == EXIT_CONFIG


class _Alive:
    """Stays running until terminate()."""

    def __init__(self):
        self.stdout = None
        self.terminated = False
        self.killed = False
        self._dead = False

    def poll(self):
        return 0 if self._dead else None

    def terminate(self):
        self.terminated = True
        self._dead = True

    def kill(self):
        self.killed = True
        self._dead = True


def _clock():
    state = {"t": 0.0}
    pauses = []

    def now():
        return state["t"]

    def pause(seconds):
        pauses.append(seconds)
        state["t"] += seconds

    return now, pause, pauses


def test_backoff_caps_at_thirty_seconds(tmp_path):
    def spawn(argv, **kwargs):
        return _Proc()

    _now, pause, pauses = _clock()
    code = run_channel(
        "A", tmp_path / "missing.json", tmp_path / "hls", tmp_path / "state",
        spawn=spawn, cycles=7, install_signals=False, clock=_now, pause=pause, notify=lambda _msg: None,
    )
    assert code == 0
    assert pauses == [2, 4, 8, 16, 30, 30]


def test_stalled_frames_restart_the_pipeline(tmp_path):
    starts = []

    def spawn(argv, **kwargs):
        proc = _Alive()
        if kwargs.get("stdout") is subprocess.PIPE:
            proc.stdout = type("Pipe", (), {"close": lambda self: None})()
        if argv[0] == "ffmpeg":
            starts.append(proc)
            (tmp_path / "state" / "A.progress").write_text("frame=10\nprogress=continue\n", encoding="utf-8")
        return proc

    _now, pause, pauses = _clock()
    code = run_channel(
        "A", tmp_path / "missing.json", tmp_path / "hls", tmp_path / "state",
        spawn=spawn, cycles=2, install_signals=False, clock=_now, pause=pause,
        notify=lambda _msg: None, stall_seconds=5,
    )
    assert code == 0
    assert len(starts) == 2
    assert all(proc.terminated for proc in starts)
    assert 5 <= sum(1 for delay in pauses if delay == 1)


def test_exec_failure_is_retried(tmp_path):
    calls = {"n": 0}

    def spawn(argv, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("ffmpeg missing")
        proc = _Proc()
        if kwargs.get("stdout") is subprocess.PIPE:
            proc.stdout = type("Pipe", (), {"close": lambda self: None})()
        return proc

    _now, pause, pauses = _clock()
    code = run_channel(
        "A", tmp_path / "missing.json", tmp_path / "hls", tmp_path / "state",
        spawn=spawn, cycles=1, install_signals=False, clock=_now, pause=pause, notify=lambda _msg: None,
    )
    assert code == 0
    assert calls["n"] == 4
    assert pauses == [2]


def test_a_dead_monitor_does_not_restart_the_program(tmp_path):
    program = []

    def spawn(argv, **kwargs):
        proc = _Proc()
        if kwargs.get("stdout") is subprocess.PIPE:
            proc.stdout = type("Pipe", (), {"close": lambda self: None})()
        if argv[0] == "ffmpeg" and "pipe:1" in argv:
            program.append(argv)
        return proc

    code = run_channel(
        "A", tmp_path / "missing.json", tmp_path / "hls", tmp_path / "state",
        spawn=spawn, cycles=1, install_signals=False, manage_preview=True,
        pause=lambda _seconds: None, notify=lambda _msg: None,
    )
    assert code == 0
    assert len(program) == 1


def test_progress_pipe_keeps_a_reader_when_fd_3_is_free():
    saved = None
    try:
        saved = os.dup(3)
    except OSError:
        saved = None
    else:
        os.close(3)
    read_fd, child_fd = open_progress_pipe()
    try:
        assert child_fd == 3
        assert read_fd != 3
        os.write(child_fd, b"frame=4\n")
        assert os.read(read_fd, 16) == b"frame=4\n"
    finally:
        os.close(read_fd)
        os.close(child_fd)
        if saved is not None:
            os.dup2(saved, 3)
            os.close(saved)


def test_progress_target_uses_a_pipe_on_the_service():
    assert progress_target(True, tmp := __import__("pathlib").Path("A.progress")) == "pipe:3"
    assert progress_target(False, tmp) == str(tmp)
