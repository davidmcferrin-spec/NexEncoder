"""Run one channel: ffmpeg capture | tsp spliceinject, plus the SRT or HLS egress.

``python -m nexencoder.channel --id A`` is what the systemd template starts.
A dead child, a failed exec, or a stalled frame counter restarts the pipeline
with backoff. A config error exits 78 so systemd leaves the unit failed until
an operator fixes it. VANC datagrams from the patched DeckLink demuxer are
translated to SCTE-35 and posted to the console.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

from .cgfeed import SlotWriter, supervise
from .channels import INPUT_SRT_PORTS, load_config
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
from .pipeline import VANC_PORT, capture_argv, clock_argv, egress_argv, splice_argv
from .signal import signal_mode
from .srtstats import atomic_json, redact_secrets, start_srt_stats_reader, tsp_supports_srt_stats
from .status import parse_ffmpeg_progress, progress_frame
from .vanc_feed import start_vanc_thread

log = logging.getLogger("nexencoder.channel")

EXIT_CONFIG = 78
STALL_SECONDS = 15.0
BACKOFF_START = 2.0
BACKOFF_CAP = 30.0


def _spawn(argv: list[str], **kwargs) -> subprocess.Popen:
    log.info("start %s", redact_secrets(" ".join(argv)))
    return subprocess.Popen(argv, **kwargs)


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


def _popen_with_progress(argv: list[str], dest: Path, stderr=None) -> subprocess.Popen:
    """Run ffmpeg with -progress pipe:3 and mirror the latest block onto dest."""
    read_fd, write_fd = os.pipe()
    try:
        if write_fd != 3:
            os.dup2(write_fd, 3)
            os.close(write_fd)
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=stderr, pass_fds=(3,), close_fds=True
        )
    except Exception:
        os.close(read_fd)
        try:
            os.close(3)
        except OSError:
            pass
        raise
    else:
        try:
            os.close(3)
        except OSError:
            pass
    threading.Thread(target=_consume_progress, args=(read_fd, dest), name="nexencoder-progress", daemon=True).start()
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
    srt_statistics: bool | None = None,
) -> int:
    config = load_config(config_path)
    spec_in = config["inputs"][channel_id]
    spec_out = config["outputs"][channel_id]
    publish = publish_root / channel_id
    publish.mkdir(parents=True, exist_ok=True)
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
        if source == "srt":
            capture = clock_argv(
                channel_id, spec_in["format"], spec_out["bitrate"],
                progress_path=progress_arg, effect_state=effect_file,
                hold_path=hold_file, pcm_path=pcm_file,
            )
        else:
            capture = capture_argv(
                channel_id, spec_in["device"], spec_in["format"], spec_out["bitrate"],
                progress_path=progress_arg, effect_state=effect_file, still_path=still_file,
                hold_path=hold_file, pcm_path=pcm_file,
            )
        splice = splice_argv(channel_id)
        stats_on = False
        if spec_out["mode"].startswith("srt-"):
            stats_on = tsp_supports_srt_stats() if srt_statistics is None else bool(srt_statistics)
        egress = egress_argv(
            channel_id,
            spec_out["mode"],
            spec_out["host"],
            spec_out["port"],
            spec_out["url"],
            str(publish),
            srt_statistics=stats_on,
        )
    except ValueError as exc:
        log.error("%s", exc)
        note_fault(state_dir, channel_id, str(exc))
        return EXIT_CONFIG

    children: list[subprocess.Popen] = []
    splice_proc: list[subprocess.Popen | None] = [None]
    stopping = False
    vanc_stop = threading.Event()
    effect_stop = threading.Event()
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
    if vanc_listen:
        os.environ["NEXENCODER_VANC_PORT"] = str(VANC_PORT[channel_id])
        start_vanc_thread(channel_id, vanc_stop, lambda: _running(splice_proc[0]))

    def stop(*_args) -> None:
        nonlocal stopping
        stopping = True
        vanc_stop.set()
        effect_stop.set()
        _stop_children(children, pause)

    if install_signals:
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

    notify("READY=1\n")
    ran = 0
    delay = backoff_start
    while not stopping and (cycles is None or ran < cycles):
        started: list[subprocess.Popen] = []
        try:
            if pipe_progress and progress_file is not None and spawn is _spawn:
                stderr = subprocess.PIPE if manage_live and source == "sdi" else None
                ffmpeg = _popen_with_progress(capture, progress_file, stderr=stderr)
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
            # Bind the program reader before spliceinject starts sending.
            srt_pipe = subprocess.PIPE if stats_on and state_dir is not None else None
            out = spawn(egress, stderr=srt_pipe)
            started.append(out)
            if srt_pipe is not None and state_dir is not None:
                start_srt_stats_reader(out, state_dir / f"{channel_id}.srt.json")
            tsp = spawn(splice, stdin=ffmpeg.stdout)
            started.append(tsp)
            splice_proc[0] = tsp
            if ffmpeg.stdout is not None:
                ffmpeg.stdout.close()
        except OSError as exc:
            log.error("channel %s failed to start: %s", channel_id, exc)
            note_fault(state_dir, channel_id, f"failed to start: {exc}")
            splice_proc[0] = None
            _stop_children(started, pause)
            notify("WATCHDOG=1\n")
            pause(delay)
            delay = min(delay * 2, backoff_cap)
            continue
        children[:] = started
        ran += 1
        note_runs(state_dir, channel_id, ran)
        opened = clock()
        last_frame = 0
        last_advance = opened
        stalled = False
        while not stopping and all(proc.poll() is None for proc in children):
            pause(1)
            notify("WATCHDOG=1\n")
            frame = progress_frame(progress_file)
            now = clock()
            if frame > last_frame:
                last_frame = frame
                last_advance = now
            elif last_frame > 0 and now - last_advance >= stall_seconds:
                log.warning("channel %s stalled at frame %s", channel_id, last_frame)
                stalled = True
                break
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
    if vanc_listen:
        if previous_port is None:
            os.environ.pop("NEXENCODER_VANC_PORT", None)
        else:
            os.environ["NEXENCODER_VANC_PORT"] = previous_port
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Run one NexEncoder channel")
    parser.add_argument("--id", required=True, choices=("A", "B"))
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("NEXENCODER_CONFIG", "/var/lib/nexencoder/config.json")))
    parser.add_argument("--hls", type=Path, default=Path(os.environ.get("NEXENCODER_HLS", "/var/lib/nexencoder/hls")))
    parser.add_argument("--state", type=Path, default=Path(os.environ.get("NEXENCODER_STATE", "/var/lib/nexencoder/state")))
    args = parser.parse_args(argv)
    return run_channel(
        args.id, args.config, args.hls, args.state,
        pipe_progress=(os.name == "posix"),
        vanc_listen=(os.name == "posix"),
        manage_effects=(os.name == "posix"),
        manage_live=(os.name == "posix"),
    )


if __name__ == "__main__":
    raise SystemExit(main())
