"""Run one channel: ffmpeg capture | tsp spliceinject, plus the SRT or HLS egress.

``python -m nexencoder.channel --id A`` is what the systemd template starts.
A dead child, a failed exec, or a stalled frame counter restarts the pipeline
with backoff. A config error exits 78 so systemd leaves the unit failed until
an operator fixes it.
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

from .channels import load_config
from .pipeline import capture_argv, egress_argv, splice_argv
from .status import parse_ffmpeg_progress, progress_frame

log = logging.getLogger("nexencoder.channel")

EXIT_CONFIG = 78
STALL_SECONDS = 15.0
BACKOFF_START = 2.0
BACKOFF_CAP = 30.0


def _spawn(argv: list[str], **kwargs) -> subprocess.Popen:
    log.info("start %s", " ".join(argv))
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


def _popen_with_progress(argv: list[str], dest: Path) -> subprocess.Popen:
    """Run ffmpeg with -progress pipe:3 and mirror the latest block onto dest."""
    read_fd, write_fd = os.pipe()
    try:
        if write_fd != 3:
            os.dup2(write_fd, 3)
            os.close(write_fd)
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, pass_fds=(3,), close_fds=True)
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
    try:
        capture = capture_argv(
            channel_id, spec_in["device"], spec_in["format"], spec_out["bitrate"], progress_path=progress_arg,
        )
        splice = splice_argv(channel_id)
        egress = egress_argv(
            channel_id,
            spec_out["mode"],
            spec_out["host"],
            spec_out["port"],
            spec_out["url"],
            str(publish),
        )
    except ValueError as exc:
        log.error("%s", exc)
        return EXIT_CONFIG

    children: list[subprocess.Popen] = []
    stopping = False

    def stop(*_args) -> None:
        nonlocal stopping
        stopping = True
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
                ffmpeg = _popen_with_progress(capture, progress_file)
            else:
                ffmpeg = spawn(capture, stdout=subprocess.PIPE)
            started.append(ffmpeg)
            tsp = spawn(splice, stdin=ffmpeg.stdout)
            started.append(tsp)
            if ffmpeg.stdout is not None:
                ffmpeg.stdout.close()
            out = spawn(egress)
            started.append(out)
        except OSError as exc:
            log.error("channel %s failed to start: %s", channel_id, exc)
            _stop_children(started, pause)
            notify("WATCHDOG=1\n")
            pause(delay)
            delay = min(delay * 2, backoff_cap)
            continue
        children[:] = started
        ran += 1
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
        if stopping or (cycles is not None and ran >= cycles):
            break
        reason = "stalled" if stalled else "exited"
        log.warning("channel %s pipeline %s; restarting in %ss", channel_id, reason, delay)
        notify("WATCHDOG=1\n")
        pause(delay)
        if last_frame > 0 and clock() - opened >= backoff_cap:
            delay = backoff_start
        else:
            delay = min(delay * 2, backoff_cap)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Run one NexEncoder channel")
    parser.add_argument("--id", required=True, choices=("A", "B"))
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("NEXENCODER_CONFIG", "/var/lib/nexencoder/config.json")))
    parser.add_argument("--hls", type=Path, default=Path(os.environ.get("NEXENCODER_HLS", "/var/lib/nexencoder/hls")))
    parser.add_argument("--state", type=Path, default=Path(os.environ.get("NEXENCODER_STATE", "/var/lib/nexencoder/state")))
    args = parser.parse_args(argv)
    return run_channel(args.id, args.config, args.hls, args.state, pipe_progress=(os.name == "posix"))


if __name__ == "__main__":
    raise SystemExit(main())
