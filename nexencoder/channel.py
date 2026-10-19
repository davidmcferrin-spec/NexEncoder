"""Run one channel: ffmpeg capture | tsp spliceinject, plus one egress per destination.

``python -m nexencoder.channel --id A`` is what the systemd template starts.
A dead child, a failed exec, or a stalled frame counter restarts the pipeline
with backoff. A config error exits 78 so systemd leaves the unit failed until
an operator fixes it. VANC datagrams from the patched DeckLink demuxer are
translated to SCTE-35 and posted to the console. Vendor caption datagrams
are written to the NXCC file the nexcaption filter reads at encode time.
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import signal
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

from .cgfeed import SlotWriter, supervise
from .channels import INPUT_SRT_PORTS, hls_publish_dir, load_config, srt_stats_path
from .effects import (
    bars_path,
    effect_path,
    layers_path,
    live_slot_path,
    loop_slot_path,
    pcm_path,
    signal_path,
    still_path,
    wait_path,
    write_flag,
    write_wait,
)
from .livefeed import (
    PcmRing,
    check_loop_file,
    ident_text,
    loop_argv,
    run_srt_ingest,
    run_video_pipe,
    srt_ingest_argv,
    watch_decklink_signal,
)
from .pipeline import (
    FEED_ADDR,
    FEED_LOCAL,
    FEED_PORT,
    FEED_RCVBUF,
    PROGRESS_FD,
    STILL_FD,
    bind_pipe_fds,
    SDI_SCTE_PORT,
    VANC_PORT,
    caption_state_path,
    capture_argv,
    clock_argv,
    egress_argv,
    preview_argv,
    sdi_argv,
    splice_argv,
)
from .signal import signal_mode
from .srtstats import atomic_json, redact_secrets, start_srt_stats_reader, tsp_supports_srt_stats
from .status import parse_ffmpeg_progress, progress_frame
from .ts import PreviewGate
from .caption_feed import start_caption_thread
from .vanc_feed import start_vanc_thread

log = logging.getLogger("nexencoder.channel")

EXIT_CONFIG = 78
STALL_SECONDS = 15.0
BACKOFF_START = 2.0
BACKOFF_CAP = 30.0
STILL_PIPE_BYTES = 1 << 20


def _spawn(argv: list[str], **kwargs) -> subprocess.Popen:
    log.info("start %s", redact_secrets(" ".join(argv)))
    return subprocess.Popen(argv, **kwargs)


def ensure_udp_receive_buffer(proc_path: Path | None = None, size: int = FEED_RCVBUF) -> None:
    """Raise the kernel UDP receive cap so the monitor's buffer_size is applied.

    Linux clamps SO_RCVBUF to rmem_max. The URL value does nothing while that
    cap stays at the default, and a dropped datagram pixelates until the next keyframe.
    """
    path = proc_path or Path("/proc/sys/net/core/rmem_max")
    try:
        current = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    if current >= size:
        return
    try:
        path.write_text(f"{size}\n", encoding="utf-8")
    except OSError:
        log.warning("UDP receive buffer stays at %s bytes", current)


def sd_notify(message: str) -> None:
    """Ping systemd when NOTIFY_SOCKET is set. A manual run has no socket."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    dest = ("\0" + addr[1:]) if addr.startswith("@") else addr
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.sendto(message.encode("utf-8"), dest)
    except OSError:
        log.warning("systemd notify failed")
    finally:
        sock.close()


def progress_target(pipe: bool, path: Path) -> str:
    """ffmpeg -progress target. A pipe stays bounded; a file is for tests."""
    return "pipe:3" if pipe else str(path)


def clear_progress(path: Path | None) -> None:
    """Drop the previous run's frame count before a new encode starts."""
    if path is None:
        return
    try:
        path.write_text("", encoding="utf-8")
    except OSError:
        log.warning("progress %s was not cleared", path)


def note_progress(
    frame: int,
    last_frame: int,
    last_advance: float,
    now: float,
    stall_seconds: float,
) -> tuple[int, float, bool]:
    """Return ``(last_frame, last_advance, stalled)`` for this encode.

    A count that goes backwards is a new process replacing a leftover file.
    A stall is this run's own count sitting still.
    """
    if frame < last_frame:
        return frame, now, False
    if frame > last_frame:
        return frame, now, False
    if last_frame > 0 and now - last_advance >= stall_seconds:
        return last_frame, last_advance, True
    return last_frame, last_advance, False


def _consume_progress(read_fd: int, dest: Path) -> None:
    """Overwrite dest with the latest progress block so the file cannot grow."""
    buf = b""
    try:
        with os.fdopen(read_fd, "rb") as handle:
            while True:
                chunk = handle.read(4096)
                if not chunk:
                    break
                buf = (buf + chunk)[-65536:]
                if b"progress=" not in buf:
                    continue
                fields = parse_ffmpeg_progress(buf.decode("utf-8", "replace"))
                if not fields:
                    continue
                body = "".join(f"{key}={value}\n" for key, value in fields.items())
                body += "progress=continue\n"
                tmp = dest.with_suffix(".tmp")
                tmp.write_text(body, encoding="utf-8")
                tmp.replace(dest)
    except OSError:
        log.warning("progress reader stopped", exc_info=True)


def open_child_pipe() -> tuple[int, int]:
    """Return ``(read_fd, write_fd)`` for a pipe the child writes.

    The write end keeps whatever number ``pipe()`` gave it. An earlier version
    moved it onto a fixed number with ``dup2``, which silently replaced that
    descriptor if another thread had it open, such as the CG feeder's devtools
    socket. The argv is rebound to the real number at spawn time instead.
    """
    return os.pipe()


def open_progress_pipe() -> tuple[int, int]:
    """Return ``(read_fd, write_fd)`` for ``ffmpeg -progress``."""
    return open_child_pipe()


def pop_jpegs(buf: bytes) -> tuple[list[bytes], bytes]:
    """Split a concatenated MJPEG stream into complete frames."""
    frames: list[bytes] = []
    while True:
        start = buf.find(b"\xff\xd8")
        if start < 0:
            return frames, buf[-1:] if buf else b""
        end = buf.find(b"\xff\xd9", start + 2)
        if end < 0:
            return frames, buf[start:]
        frames.append(buf[start:end + 2])
        buf = buf[end + 2:]


def write_jpeg_atomic(dest: Path, frame: bytes) -> None:
    """Replace dest with one JPEG. An OSError leaves the previous file in place."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(frame)
    tmp.replace(dest)


def _keep_latest(pending: queue.Queue, item: bytes | None) -> None:
    """Hold one item. A newer frame replaces one the disk writer has not taken."""
    try:
        pending.put_nowait(item)
        return
    except queue.Full:
        pass
    try:
        pending.get_nowait()
    except queue.Empty:
        pass
    try:
        pending.put_nowait(item)
    except queue.Full:
        pass


def _consume_still(read_fd: int, dest: Path) -> None:
    """Drain the still pipe. Disk writes stay off this read.

    A slow replace used to fill the pipe and stop the program filter graph,
    which is the DeckLink overrun. Only the latest JPEG is kept.
    """
    pending: queue.Queue = queue.Queue(maxsize=1)

    def write_latest() -> None:
        while True:
            frame = pending.get()
            if frame is None:
                return
            try:
                write_jpeg_atomic(dest, frame)
            except OSError:
                log.warning("still %s was not replaced", dest)

    threading.Thread(target=write_latest, name="nexencoder-still-disk", daemon=True).start()
    buf = b""
    try:
        while True:
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 8_000_000:
                buf = buf[-4_000_000:]
            frames, buf = pop_jpegs(buf)
            for frame in frames:
                _keep_latest(pending, frame)
    except OSError:
        log.warning("still reader stopped", exc_info=True)
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass
        _keep_latest(pending, None)


def widen_pipe(fd: int, size: int = STILL_PIPE_BYTES, what: str = "still") -> None:
    """Give the pipe room so one write cannot fill it and stall the writer."""
    try:
        import fcntl
    except ImportError:
        return
    flag = getattr(fcntl, "F_SETPIPE_SZ", None)
    if flag is None:
        return
    try:
        fcntl.fcntl(fd, flag, size)
    except OSError:
        log.warning("%s pipe was not widened", what)


def _popen_with_progress(argv: list[str], dest: Path, stderr=None, still_path: Path | None = None) -> subprocess.Popen:
    """Run ffmpeg with -progress on a pipe and, for SDI, the still on another.

    The argv names them ``pipe:3`` and ``pipe:4``; the real descriptors are
    bound here and handed over with ``pass_fds`` so their numbers survive exec.
    """
    read_fd, child_fd = open_progress_pipe()
    still_read = None
    still_fd = None
    pass_fds = [child_fd]
    bound = {PROGRESS_FD: child_fd}
    if still_path is not None:
        still_read, still_fd = open_child_pipe()
        widen_pipe(still_read)
        pass_fds.append(still_fd)
        bound[STILL_FD] = still_fd
    try:
        proc = subprocess.Popen(
            bind_pipe_fds(argv, bound), stdout=subprocess.PIPE, stderr=stderr,
            pass_fds=tuple(pass_fds), close_fds=True,
        )
    except Exception:
        os.close(read_fd)
        if still_read is not None:
            os.close(still_read)
        for fd in pass_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        raise
    else:
        for fd in pass_fds:
            try:
                os.close(fd)
            except OSError:
                pass
    threading.Thread(target=_consume_progress, args=(read_fd, dest), name="nexencoder-progress", daemon=True).start()
    if still_path is not None and still_read is not None:
        threading.Thread(
            target=_consume_still, args=(still_read, still_path), name="nexencoder-still", daemon=True,
        ).start()
    return proc


def _stop_children(children: list[subprocess.Popen], pause) -> None:
    live = [proc for proc in children if proc.poll() is None]
    for proc in live:
        proc.terminate()
    if not live:
        return
    pause(2)
    for proc in live:
        if proc.poll() is None:
            proc.kill()


def _running(proc: subprocess.Popen | None) -> bool:
    return proc is not None and proc.poll() is None


_PREVIEW_WAIT = ("non-existing PPS", "no frame!", "decode_slice_header error")


def _join_program_feed(channel_id: str) -> socket.socket:
    """Join the program multicast on loopback, sharing the port with SRT and HLS."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, FEED_RCVBUF)
    except OSError:
        log.warning("channel %s monitor could not raise its receive buffer", channel_id)
    sock.bind(("", FEED_PORT[channel_id]))
    membership = struct.pack("4s4s", socket.inet_aton(FEED_ADDR), socket.inet_aton(FEED_LOCAL))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
    sock.settimeout(0.5)
    return sock


def _close_stdin(proc) -> None:
    stdin = getattr(proc, "stdin", None)
    if stdin is None:
        return
    try:
        stdin.close()
    except OSError:
        pass


def _drain_preview(pipe, channel_id: str) -> None:
    """Keep one waiting line. A slice before the keyframe is not a new fault."""
    noted = False
    try:
        for raw in iter(pipe.readline, b""):
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            if any(mark in text for mark in _PREVIEW_WAIT):
                if not noted:
                    log.warning("channel %s monitor is waiting for a keyframe", channel_id)
                    noted = True
                continue
            log.warning("channel %s monitor %s", channel_id, text)
    except OSError:
        return


def _feed_preview(channel_id: str, proc, stop: threading.Event) -> None:
    """Write the program feed to the monitor, starting at a video keyframe."""
    try:
        sock = _join_program_feed(channel_id)
    except OSError as exc:
        log.warning("channel %s monitor could not join the program feed: %s", channel_id, exc)
        _close_stdin(proc)
        return
    gate = PreviewGate()
    waiting_noted = False
    opened_noted = False
    started = time.monotonic()
    try:
        stdin = getattr(proc, "stdin", None)
        if stdin is not None:
            try:
                widen_pipe(stdin.fileno(), what="monitor")
            except OSError:
                pass
        while proc.poll() is None and not stop.is_set():
            try:
                data = sock.recv(65535)
            except TimeoutError:
                data = b""
            except OSError:
                break
            if data and stdin is not None:
                out = gate.feed(data)
                if out:
                    try:
                        stdin.write(out)
                        stdin.flush()
                    except OSError:
                        break
            if gate.opened:
                if not opened_noted:
                    log.info("channel %s monitor started at a keyframe", channel_id)
                    opened_noted = True
            elif not waiting_noted and time.monotonic() - started >= 2:
                log.warning("channel %s monitor is waiting for a keyframe", channel_id)
                waiting_noted = True
    finally:
        sock.close()
        _close_stdin(proc)


def run_preview(channel_id: str, stop: threading.Event, spawn, pause) -> None:
    """Publish the monitor from the program feed until stop is set.

    This process is not one of the encode children. When it exits, only
    the monitor is started again. The SRT output keeps running. Bytes reach
    it only after a video keyframe, so a join does not decode a bare slice.
    """
    while not stop.is_set():
        try:
            proc = spawn(
                preview_argv(channel_id, source="pipe:0"),
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            log.warning("channel %s preview failed to start: %s", channel_id, exc)
            pause(1)
            continue
        stderr = getattr(proc, "stderr", None)
        if stderr is not None:
            threading.Thread(
                target=_drain_preview, args=(stderr, channel_id),
                name="nexencoder-preview-log", daemon=True,
            ).start()
        if getattr(proc, "stdin", None) is not None:
            threading.Thread(
                target=_feed_preview, args=(channel_id, proc, stop),
                name=f"nexencoder-preview-{channel_id}", daemon=True,
            ).start()
        while proc.poll() is None and not stop.is_set():
            stop.wait(0.5)
        _close_stdin(proc)
        if stop.is_set():
            _stop_children([proc], pause)
            return
        log.warning("channel %s preview exited code=%s; restarting", channel_id, proc.poll())
        pause(1)


def watch_egress(
    proc: subprocess.Popen | None,
    relaunch,
    stop: threading.Event,
    hold: threading.Event,
    pause,
    label: str,
    backoff_start: float = BACKOFF_START,
    backoff_cap: float = BACKOFF_CAP,
) -> None:
    """Restart one destination when it exits while the encode is up.

    ``hold`` is set while the encode itself is down or starting. A dead
    destination waits out that window instead of flapping beside a restart.
    ``stop`` ends the channel and tears this process down.
    """
    delay = backoff_start
    current = proc
    while not stop.is_set():
        if current is not None and current.poll() is None:
            if stop.wait(0.5):
                break
            continue
        if hold.is_set():
            if stop.wait(0.05):
                break
            continue
        log.warning("%s exited; restarting in %ss", label, delay)
        if stop.wait(delay):
            break
        try:
            current = relaunch()
        except OSError as exc:
            log.error("%s failed to start: %s", label, exc)
            delay = min(delay * 2, backoff_cap)
            current = None
            continue
        delay = backoff_start
    if current is not None:
        _stop_children([current], pause)


def note_fault(state_dir: Path | None, channel_id: str, message: str) -> None:
    """Remember the latest pipeline fault for the NOC agent."""
    if state_dir is None:
        return
    text = " ".join(message.split())[:500]
    atomic_json(state_dir / f"{channel_id}.error", {"message": text, "unix": time.time()})


def note_runs(state_dir: Path | None, channel_id: str, runs: int) -> None:
    """How many times this process has spawned the encode pipeline."""
    if state_dir is None:
        return
    path = state_dir / f"{channel_id}.runs"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(f"{int(runs)}\n", encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o644)
    except OSError:
        pass


def run_channel(
    channel_id: str,
    config_path: Path,
    publish_root: Path,
    state_dir: Path | None = None,
    spawn=_spawn,
    cycles: int | None = None,
    pause=time.sleep,
    install_signals: bool = True,
    clock=time.monotonic,
    notify=sd_notify,
    stall_seconds: float = STALL_SECONDS,
    backoff_start: float = BACKOFF_START,
    backoff_cap: float = BACKOFF_CAP,
    pipe_progress: bool = False,
    vanc_listen: bool = False,
    manage_effects: bool = False,
    manage_live: bool = False,
    manage_preview: bool = False,
    srt_statistics: bool | None = None,
) -> int:
    config = load_config(config_path)
    spec_in = config["inputs"][channel_id]
    destinations = config["outputs"][channel_id]
    bitrate = int(spec_in["bitrate"])
    progress_file = None
    progress_arg = None
    if state_dir is not None:
        state_dir.mkdir(parents=True, exist_ok=True)
        progress_file = state_dir / f"{channel_id}.progress"
        progress_arg = progress_target(pipe_progress, progress_file)
    effect_file = str(effect_path(state_dir, channel_id)) if state_dir is not None else None
    still_file = str(still_path(state_dir, channel_id)) if state_dir is not None else None
    source = spec_in.get("source", "sdi")
    hold_file = str(wait_path(state_dir, channel_id)) if state_dir is not None else None
    pcm_file = None
    waiting = spec_in.get("waiting", "slate")
    loop = spec_in.get("loop", "") or ""
    if state_dir is not None:
        if waiting == "loop":
            try:
                check_loop_file(loop)
            except ValueError as exc:
                log.warning("channel %s %s; using the waiting slate", channel_id, exc)
                waiting = "slate"
                loop = ""
        live = str(live_slot_path(state_dir, channel_id)) if source == "srt" else ""
        shown = str(loop_slot_path(state_dir, channel_id)) if waiting == "loop" else ""
        write_wait(state_dir, channel_id, waiting, ident_text(channel_id), shown, live)
        if not bars_path(state_dir, channel_id).is_file():
            write_flag(bars_path(state_dir, channel_id), False)
        write_flag(signal_path(state_dir, channel_id), source == "sdi")
        if source == "srt":
            pcm_file = str(pcm_path(state_dir, channel_id))
    try:
        caption_file = caption_state_path(channel_id, str(state_dir) if state_dir is not None else None)
        if source == "srt":
            capture = clock_argv(
                channel_id, spec_in["format"], bitrate,
                progress_path=progress_arg, effect_state=effect_file,
                hold_path=hold_file, pcm_path=pcm_file, caption_path=caption_file,
            )
        else:
            capture = capture_argv(
                channel_id, spec_in["device"], spec_in["format"], bitrate,
                progress_path=progress_arg, effect_state=effect_file, still_path=still_file,
                hold_path=hold_file, pcm_path=pcm_file, caption_path=caption_file,
            )
        splice = splice_argv(channel_id)
        wants_srt = any(item["mode"].startswith("srt-") for item in destinations)
        stats_on = False
        if wants_srt:
            stats_on = tsp_supports_srt_stats() if srt_statistics is None else bool(srt_statistics)
        plans = []
        for spec in destinations:
            slot = spec["id"]
            if spec["mode"] == "sdi":
                plans.append({
                    "spec": spec,
                    "argv": sdi_argv(channel_id, device=spec.get("device") or None),
                    "stats": False,
                    "publish": None,
                })
                continue
            publish = hls_publish_dir(publish_root, channel_id, slot)
            publish.mkdir(parents=True, exist_ok=True)
            stats = stats_on and spec["mode"].startswith("srt-")
            argv = egress_argv(
                channel_id,
                spec["mode"],
                spec["host"],
                spec["port"],
                spec["url"],
                str(publish),
                srt_statistics=stats,
            )
            plans.append({"spec": spec, "argv": argv, "stats": stats, "publish": publish})
    except ValueError as exc:
        log.error("%s", exc)
        note_fault(state_dir, channel_id, str(exc))
        return EXIT_CONFIG

    children: list[subprocess.Popen] = []
    splice_proc: list[subprocess.Popen | None] = [None]
    stopping = False
    vanc_stop = threading.Event()
    effect_stop = threading.Event()
    preview_stop = threading.Event()
    egress_stop = threading.Event()
    # Set while the encode is down so a dead destination does not restart beside it.
    egress_hold = threading.Event()
    egress_hold.set()
    egress_threads: list[threading.Thread] = []
    watchers_started = False
    preview_thread = None
    if manage_preview:
        preview_thread = threading.Thread(
            target=run_preview,
            args=(channel_id, preview_stop, spawn, pause),
            name="nexencoder-preview",
            daemon=True,
        )
        preview_thread.start()
        log.info("channel %s monitor reading the program feed", channel_id)
    if manage_effects and state_dir is not None:
        threading.Thread(
            target=supervise,
            args=(state_dir, channel_id, effect_stop),
            name="nexencoder-cg",
            daemon=True,
        ).start()
        log.info("channel %s CG supervisor watching %s", channel_id, layers_path(state_dir, channel_id))
    if manage_live and state_dir is not None and source == "srt":
        mode = signal_mode(spec_in["format"])
        threading.Thread(
            target=run_srt_ingest,
            args=(
                srt_ingest_argv(INPUT_SRT_PORTS[channel_id], spec_in["format"], still_file or ""),
                SlotWriter(live_slot_path(state_dir, channel_id), mode.width, mode.height),
                PcmRing(pcm_path(state_dir, channel_id)),
                signal_path(state_dir, channel_id),
                effect_stop,
            ),
            name="nexencoder-srt",
            daemon=True,
        ).start()
    if manage_live and state_dir is not None and waiting == "loop" and loop:
        mode = signal_mode(spec_in["format"])
        threading.Thread(
            target=run_video_pipe,
            args=(
                loop_argv(loop, spec_in["format"]),
                SlotWriter(loop_slot_path(state_dir, channel_id), mode.width, mode.height),
                effect_stop,
            ),
            name="nexencoder-loop",
            daemon=True,
        ).start()
    previous_port = os.environ.get("NEXENCODER_VANC_PORT")
    previous_sdi = os.environ.get("NEXENCODER_SDI_SCTE_PORT")
    if any(item["mode"] == "sdi" for item in destinations):
        os.environ["NEXENCODER_SDI_SCTE_PORT"] = str(SDI_SCTE_PORT[channel_id])
    if vanc_listen:
        os.environ["NEXENCODER_VANC_PORT"] = str(VANC_PORT[channel_id])
        start_vanc_thread(channel_id, vanc_stop, lambda: _running(splice_proc[0]))
        if state_dir is not None:
            start_caption_thread(channel_id, state_dir, vanc_stop)

    def stop(*_args) -> None:
        nonlocal stopping
        stopping = True
        vanc_stop.set()
        effect_stop.set()
        preview_stop.set()
        egress_stop.set()
        egress_hold.set()
        _stop_children(children, pause)

    if install_signals:
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

    def launch_egress(plan: dict) -> subprocess.Popen:
        pipe = subprocess.PIPE if plan["stats"] and state_dir is not None else None
        proc = spawn(plan["argv"], stderr=pipe)
        if pipe is not None and state_dir is not None:
            start_srt_stats_reader(proc, srt_stats_path(state_dir, channel_id, plan["spec"]["id"]))
        return proc

    notify("READY=1\n")
    ran = 0
    delay = backoff_start
    while not stopping and (cycles is None or ran < cycles):
        clear_progress(progress_file)
        started: list[subprocess.Popen] = []
        try:
            if pipe_progress and progress_file is not None and spawn is _spawn:
                stderr = subprocess.PIPE if manage_live and source == "sdi" else None
                ffmpeg = _popen_with_progress(
                    capture, progress_file, stderr=stderr,
                    still_path=Path(still_file) if source == "sdi" and still_file else None,
                )
            else:
                extra = {}
                if manage_live and source == "sdi":
                    extra["stderr"] = subprocess.PIPE
                ffmpeg = spawn(capture, stdout=subprocess.PIPE, **extra)
            started.append(ffmpeg)
            if manage_live and source == "sdi" and state_dir is not None and ffmpeg.stderr is not None:
                threading.Thread(
                    target=watch_decklink_signal,
                    args=(ffmpeg.stderr, signal_path(state_dir, channel_id), effect_stop),
                    name="nexencoder-signal",
                    daemon=True,
                ).start()
            # Bind every program reader before spliceinject starts sending.
            # A later encode restart leaves a destination that is still up alone.
            fresh: list[tuple[dict, subprocess.Popen]] = []
            if not watchers_started:
                for plan in plans:
                    proc = launch_egress(plan)
                    started.append(proc)
                    fresh.append((plan, proc))
            tsp = spawn(splice, stdin=ffmpeg.stdout)
            started.append(tsp)
            splice_proc[0] = tsp
            if ffmpeg.stdout is not None:
                ffmpeg.stdout.close()
            if not watchers_started:
                for plan, proc in fresh:
                    slot = plan["spec"]["id"]
                    thread = threading.Thread(
                        target=watch_egress,
                        kwargs={
                            "proc": proc,
                            "relaunch": lambda plan=plan: launch_egress(plan),
                            "stop": egress_stop,
                            "hold": egress_hold,
                            "pause": pause,
                            "label": f"channel {channel_id} output {slot}",
                            "backoff_start": backoff_start,
                            "backoff_cap": backoff_cap,
                        },
                        name=f"nexencoder-egress-{channel_id}-{slot}",
                        daemon=True,
                    )
                    thread.start()
                    egress_threads.append(thread)
                watchers_started = True
                log.info("channel %s publishing %s destination(s)", channel_id, len(plans))
        except OSError as exc:
            log.error("channel %s failed to start: %s", channel_id, exc)
            note_fault(state_dir, channel_id, f"failed to start: {exc}")
            splice_proc[0] = None
            _stop_children(started, pause)
            notify("WATCHDOG=1\n")
            pause(delay)
            delay = min(delay * 2, backoff_cap)
            continue
        children[:] = [ffmpeg, tsp]
        ran += 1
        note_runs(state_dir, channel_id, ran)
        opened = clock()
        last_frame = 0
        last_advance = opened
        stalled = False
        if all(proc.poll() is None for proc in children):
            egress_hold.clear()
        while not stopping and all(proc.poll() is None for proc in children):
            pause(1)
            notify("WATCHDOG=1\n")
            frame = progress_frame(progress_file)
            now = clock()
            last_frame, last_advance, stalled = note_progress(
                frame, last_frame, last_advance, now, stall_seconds,
            )
            if stalled:
                log.warning("channel %s stalled at frame %s", channel_id, last_frame)
                break
        egress_hold.set()
        _stop_children(children, pause)
        splice_proc[0] = None
        if stopping or (cycles is not None and ran >= cycles):
            break
        reason = "stalled" if stalled else "exited"
        if stalled:
            fault = f"pipeline stalled at frame {last_frame}"
        else:
            codes = [proc.poll() for proc in children]
            fault = f"pipeline exited codes={codes}"
        note_fault(state_dir, channel_id, fault)
        log.warning("channel %s pipeline %s; restarting in %ss", channel_id, reason, delay)
        notify("WATCHDOG=1\n")
        pause(delay)
        if last_frame > 0 and clock() - opened >= backoff_cap:
            delay = backoff_start
        else:
            delay = min(delay * 2, backoff_cap)
    vanc_stop.set()
    effect_stop.set()
    preview_stop.set()
    egress_stop.set()
    egress_hold.set()
    for thread in egress_threads:
        thread.join(timeout=5)
    if preview_thread is not None:
        preview_thread.join(timeout=5)
    if vanc_listen:
        if previous_port is None:
            os.environ.pop("NEXENCODER_VANC_PORT", None)
        else:
            os.environ["NEXENCODER_VANC_PORT"] = previous_port
    if any(item["mode"] == "sdi" for item in destinations):
        if previous_sdi is None:
            os.environ.pop("NEXENCODER_SDI_SCTE_PORT", None)
        else:
            os.environ["NEXENCODER_SDI_SCTE_PORT"] = previous_sdi
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Run one NexEncoder channel")
    parser.add_argument("--id", required=True, choices=("A", "B"))
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("NEXENCODER_CONFIG", "/var/lib/nexencoder/config.json")))
    parser.add_argument("--hls", type=Path, default=Path(os.environ.get("NEXENCODER_HLS", "/var/lib/nexencoder/hls")))
    parser.add_argument("--state", type=Path, default=Path(os.environ.get("NEXENCODER_STATE", "/var/lib/nexencoder/state")))
    args = parser.parse_args(argv)
    if os.name == "posix":
        ensure_udp_receive_buffer()
    return run_channel(
        args.id, args.config, args.hls, args.state,
        pipe_progress=(os.name == "posix"),
        vanc_listen=(os.name == "posix"),
        manage_effects=(os.name == "posix"),
        manage_live=(os.name == "posix"),
        manage_preview=(os.name == "posix"),
    )


if __name__ == "__main__":
    raise SystemExit(main())
