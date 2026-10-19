"""Capture, splice, and delivery argv. Nothing here opens a DeckLink device."""

import io
import logging
import os
import subprocess
import threading

import pytest

from nexencoder import scte104 as s104
from nexencoder.channel import (
    EXIT_CONFIG,
    _consume_still,
    _drain_preview,
    clear_progress,
    ensure_udp_receive_buffer,
    note_progress,
    open_progress_pipe,
    pop_jpegs,
    progress_target,
    run_channel,
    watch_egress,
    run_preview,
    write_jpeg_atomic,
)
from nexencoder.channels import save_config
from nexencoder.pipeline import (
    SRT_STATS_INTERVAL_MS,
    SRT_STATS_PREFIX,
    bind_pipe_fds,
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
    assert "image2pipe" in argv
    assert "pipe:4" in argv
    assert not any(str(item).endswith(".jpg") for item in argv)
    params = argv[argv.index("-x264-params") + 1]
    assert params == "tff=1:sliced-threads=1:repeat-headers=1"


def test_progressive_capture_skips_interlace_flags():
    argv = capture_argv("B", "DeckLink Duo (2)", "720p59.94", 12)
    assert argv[argv.index("-format_code") + 1] == "hp59"
    assert "+ildct+ilme" not in argv
    assert argv[argv.index("-x264-params") + 1] == "sliced-threads=1:repeat-headers=1"
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
    preview_in = argv[argv.index("-i") + 1]
    assert preview_in.startswith("udp://239.255.1.1:19001?localaddr=127.0.0.1")
    assert "buffer_size=8388608" in preview_in
    assert "nexeffect" not in " ".join(argv)
    assert "decklink" not in argv
    assert argv[argv.index("-f") + 1] == "mpegts"
    assert argv[-1] == "rtsp://127.0.0.1:8554/inputA"
    assert argv[argv.index("-pkt_size") + 1] == "1316"
    assert "libopus" in argv
    assert "yadif=" in argv[argv.index("-vf") + 1]
    assert argv[argv.index("-analyzeduration") + 1] == "1000000"
    piped = preview_argv("A", source="pipe:0")
    assert piped[piped.index("-i") + 1] == "pipe:0"
    other = preview_argv("B")
    assert other[other.index("-i") + 1].startswith("udp://239.255.1.1:19002?localaddr=127.0.0.1")
    assert other[-1] == "rtsp://127.0.0.1:8554/inputB"


def test_preview_exit_restarts_only_the_monitor():
    calls = []
    stop = threading.Event()

    def spawn(argv, **kwargs):
        calls.append((argv, kwargs))
        return _Proc()

    def pause(_seconds):
        if len(calls) >= 2:
            stop.set()

    run_preview("A", stop, spawn, pause)
    assert len(calls) == 2
    assert all(item[-1] == "rtsp://127.0.0.1:8554/inputA" for item, _kwargs in calls)
    assert all("pipe:1" not in item for item, _kwargs in calls)
    assert all(item[item.index("-i") + 1] == "pipe:0" for item, _kwargs in calls)
    assert all(kwargs["stdin"] is subprocess.PIPE for _item, kwargs in calls)


def test_preview_log_keeps_one_keyframe_line(caplog):
    caplog.set_level(logging.WARNING)
    _drain_preview(io.BytesIO(
        b"non-existing PPS 0 referenced\n"
        b"no frame!\n"
        b"[rtsp @ 0x1] Connection timed out: Broken pipe\n"
    ), "A")
    text = [record.message for record in caplog.records]
    assert text.count("channel A monitor is waiting for a keyframe") == 1
    assert any("Broken pipe" in message for message in text)
    assert not any("non-existing PPS" in message for message in text)


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


def test_run_channel_starts_one_egress_per_destination(tmp_path):
    save_config(tmp_path / "config.json", {
        "outputs": {"A": [
            {"mode": "srt-listener", "port": 9000},
            {"mode": "hls-pull", "url": ""},
        ]},
    })
    calls = []

    def spawn(argv, **kwargs):
        proc = _Proc()
        if kwargs.get("stdout") is subprocess.PIPE:
            proc.stdout = type("Pipe", (), {"close": lambda self: None})()
        calls.append(argv)
        return proc

    code = run_channel(
        "A", tmp_path / "config.json", tmp_path / "hls", tmp_path / "state",
        spawn=spawn, cycles=1, install_signals=False, srt_statistics=False,
    )
    assert code == 0
    assert [argv[0] for argv in calls] == ["ffmpeg", "tsp", "ffmpeg", "tsp"]
    assert "--listener" in calls[1]
    joined = " ".join(calls[2])
    assert "hls" in joined and str(tmp_path / "hls" / "A" / "2") in joined
    assert "spliceinject" in calls[3]


def test_watch_egress_restarts_only_while_the_encode_is_up():
    launched = []
    stop = threading.Event()
    hold = threading.Event()
    hold.set()

    def relaunch():
        proc = _Alive()
        launched.append(proc)
        stop.set()
        return proc

    thread = threading.Thread(
        target=watch_egress,
        kwargs={
            "proc": _Proc(),
            "relaunch": relaunch,
            "stop": stop,
            "hold": hold,
            "pause": lambda _seconds: None,
            "label": "output 2",
            "backoff_start": 0,
            "backoff_cap": 0,
        },
        daemon=True,
    )
    thread.start()
    thread.join(0.15)
    assert thread.is_alive()
    assert launched == []
    hold.clear()
    thread.join(2)
    assert len(launched) == 1 and launched[0].terminated
    assert not thread.is_alive()


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


def test_note_progress_treats_a_lower_count_as_a_new_run():
    last, advance, stalled = note_progress(944, 0, 0.0, 1.0, 15.0)
    assert (last, stalled) == (944, False)
    last, advance, stalled = note_progress(30, last, advance, 2.0, 15.0)
    assert (last, advance, stalled) == (30, 2.0, False)
    last, advance, stalled = note_progress(60, last, advance, 3.0, 15.0)
    assert stalled is False and last == 60
    last, advance, stalled = note_progress(60, last, advance, 18.0, 15.0)
    assert stalled is True and last == 60


def test_leftover_progress_does_not_kill_the_new_run(tmp_path):
    progress = tmp_path / "state" / "A.progress"
    progress.parent.mkdir(parents=True)
    progress.write_text("frame=944\nprogress=continue\n", encoding="utf-8")
    sequence = [944, 30, 60, 90, 120, 150, 180]
    index = {"i": 0}
    starts = []

    def spawn(argv, **kwargs):
        proc = _Alive()
        if kwargs.get("stdout") is subprocess.PIPE:
            proc.stdout = type("Pipe", (), {"close": lambda self: None})()
        if argv[0] == "ffmpeg":
            starts.append(proc)
        return proc

    state = {"t": 0.0}
    pauses = []

    def pause(seconds):
        pauses.append(seconds)
        state["t"] += seconds
        if seconds == 1 and index["i"] < len(sequence):
            progress.write_text(
                f"frame={sequence[index['i']]}\nprogress=continue\n", encoding="utf-8",
            )
            index["i"] += 1

    run_channel(
        "A", tmp_path / "missing.json", tmp_path / "hls", tmp_path / "state",
        spawn=spawn, cycles=1, install_signals=False, clock=lambda: state["t"], pause=pause,
        notify=lambda _msg: None, stall_seconds=5,
    )
    assert len(starts) == 1
    assert progress.read_text(encoding="utf-8") != "frame=944\nprogress=continue\n"
    assert sum(1 for delay in pauses if delay == 1) >= 12


def test_udp_receive_buffer_is_raised_when_the_kernel_limit_is_low(tmp_path):
    path = tmp_path / "rmem_max"
    path.write_text("212992\n", encoding="utf-8")
    ensure_udp_receive_buffer(path)
    assert int(path.read_text(encoding="utf-8")) == 8388608
    path.write_text("16777216\n", encoding="utf-8")
    ensure_udp_receive_buffer(path)
    assert int(path.read_text(encoding="utf-8")) == 16777216
    ensure_udp_receive_buffer(tmp_path / "missing")


def test_clear_progress_drops_the_previous_count(tmp_path):
    path = tmp_path / "A.progress"
    path.write_text("frame=944\nprogress=continue\n", encoding="utf-8")
    clear_progress(path)
    assert path.read_text(encoding="utf-8") == ""
    clear_progress(None)


def test_a_slow_still_replace_does_not_stop_the_reader(tmp_path, monkeypatch):
    held = threading.Event()
    release = threading.Event()
    wrote = []

    def slow(dest, frame):
        wrote.append(frame)
        if len(wrote) == 1:
            held.set()
            assert release.wait(2)

    monkeypatch.setattr("nexencoder.channel.write_jpeg_atomic", slow)
    read_fd, write_fd = os.pipe()
    worker = threading.Thread(target=_consume_still, args=(read_fd, tmp_path / "A.jpg"), daemon=True)
    worker.start()
    first = b"\xff\xd8" + b"a" * 1000 + b"\xff\xd9"
    os.write(write_fd, first)
    assert held.wait(2)
    rest = b"".join(b"\xff\xd8" + bytes([n]) * 80_000 + b"\xff\xd9" for n in (2, 3, 4))
    pushed = threading.Event()

    def push():
        os.write(write_fd, rest)
        pushed.set()

    threading.Thread(target=push, daemon=True).start()
    assert pushed.wait(1)
    os.close(write_fd)
    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert wrote[0].startswith(b"\xff\xd8a")


def test_still_frames_are_split_and_replaced(tmp_path):
    first = b"\xff\xd8" + b"one" + b"\xff\xd9"
    second = b"\xff\xd8" + b"two" + b"\xff\xd9"
    frames, rest = pop_jpegs(first + second + b"\xff\xd8partial")
    assert frames == [first, second]
    assert rest == b"\xff\xd8partial"
    dest = tmp_path / "A.jpg"
    write_jpeg_atomic(dest, first)
    write_jpeg_atomic(dest, second)
    assert dest.read_bytes() == second
    assert not (tmp_path / "A.jpg.tmp").exists()


def test_progress_pipe_leaves_other_threads_descriptors_alone():
    """A devtools socket on fd 3 must survive the capture spawning."""
    import socket

    saved = None
    try:
        saved = os.dup(3)
    except OSError:
        saved = None
    else:
        os.close(3)
    keeper = socket.socket()
    try:
        read_fd, child_fd = open_progress_pipe()
        try:
            assert child_fd != keeper.fileno()
            assert read_fd != keeper.fileno()
            os.write(child_fd, b"frame=4\n")
            assert os.read(read_fd, 16) == b"frame=4\n"
            # The socket is still open and still usable.
            keeper.setblocking(False)
            assert keeper.fileno() >= 0
        finally:
            os.close(read_fd)
            os.close(child_fd)
    finally:
        keeper.close()
        if saved is not None:
            os.dup2(saved, 3)
            os.close(saved)


def test_pipe_names_are_bound_to_the_real_descriptors():
    argv = ["ffmpeg", "-progress", "pipe:3", "-f", "mjpeg", "pipe:4", "-f", "mpegts", "pipe:1"]
    bound = bind_pipe_fds(argv, {3: 17, 4: 23})
    assert bound == ["ffmpeg", "-progress", "pipe:17", "-f", "mjpeg", "pipe:23", "-f", "mpegts", "pipe:1"]
    assert bind_pipe_fds(argv, {}) == argv


def test_progress_target_uses_a_pipe_on_the_service():
    assert progress_target(True, tmp := __import__("pathlib").Path("A.progress")) == "pipe:3"
    assert progress_target(False, tmp) == str(tmp)
