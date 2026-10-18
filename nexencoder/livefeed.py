"""SRT ingest, the waiting-loop feeder, and the DeckLink signal log.

The encode process stays up on its own clock. An SRT caller is a second
process that writes a live picture and a PCM ring. DeckLink keeps delivering
frames when the SDI input unlocks; the warning line on stderr is what flips
the picture to the waiting layer.
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import subprocess
import threading
from pathlib import Path

from .cgfeed import SlotWriter
from .effects import STILL_SECONDS
from .pipeline import PREVIEW_SIZE
from .signal import lavfi_rate, signal_mode

log = logging.getLogger("nexencoder.live")

AUDIO_FD = 7
PCM_MAGIC = b"NXPC"
PCM_CAP = 48000
PCM_HEADER = 12


def host_ipv4() -> str:
    """The address a remote SRT caller would use to reach this host."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        address = sock.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        sock.close()
    return address or "0.0.0.0"


def ident_text(channel_id: str, address: str | None = None) -> str:
    number = "1" if channel_id == "A" else "2"
    return f"{address or host_ipv4()} INPUT {number}"


def signal_from_decklink_log(line: str) -> bool | None:
    """True when the SDI input locks, False when it drops, None otherwise."""
    if "No input signal detected" in line:
        return False
    if "Input returned" in line:
        return True
    return None


def srt_url(port: int) -> str:
    return f"srt://0.0.0.0:{int(port)}?mode=listener&transtype=live&latency=1500"


def srt_ingest_argv(port: int, format_name: str, still_path: str, ffmpeg: str = "ffmpeg") -> list[str]:
    """Listen for one SRT caller and write raw video, PCM, and a clean still."""
    mode = signal_mode(format_name)
    rate = lavfi_rate(mode)
    graph = (
        f"[0:v]scale={mode.width}:{mode.height}:force_original_aspect_ratio=decrease,"
        f"pad={mode.width}:{mode.height}:(ow-iw)/2:(oh-ih)/2,fps={rate},format=bgra,split=2[vraw][vstill];"
        f"[vstill]fps=1/{STILL_SECONDS},"
        f"scale={PREVIEW_SIZE}:force_original_aspect_ratio=decrease,format=yuv420p[vjpg]"
    )
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "info",
        "-i", srt_url(port),
        "-filter_complex", graph,
        "-map", "[vraw]", "-f", "rawvideo", "-pix_fmt", "bgra", "pipe:1",
        "-map", "0:a?", "-ac", "2", "-ar", "48000", "-f", "s16le", f"pipe:{AUDIO_FD}",
        "-map", "[vjpg]", "-an",
        "-c:v", "mjpeg", "-q:v", "5",
        "-f", "image2", "-update", "1", "-atomic_writing", "1",
        still_path,
    ]


def loop_argv(path: str, format_name: str, ffmpeg: str = "ffmpeg") -> list[str]:
    """Loop a 10–30 second trouble slide or branded picture at the channel rate."""
    mode = signal_mode(format_name)
    rate = lavfi_rate(mode)
    video = (
        f"scale={mode.width}:{mode.height}:force_original_aspect_ratio=decrease,"
        f"pad={mode.width}:{mode.height}:(ow-iw)/2:(oh-ih)/2,fps={rate},format=bgra"
    )
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning",
        "-stream_loop", "-1", "-re", "-i", path,
        "-an", "-vf", video,
        "-f", "rawvideo", "-pix_fmt", "bgra", "pipe:1",
    ]


def media_duration_seconds(path: str, runner=None) -> float | None:
    """Parse ffmpeg's Duration line. None when ffmpeg cannot read the file."""
    if runner is None:
        runner = subprocess.run
    try:
        done = runner(
            ["ffmpeg", "-hide_banner", "-i", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = getattr(done, "stdout", b"") or b""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    for line in text.splitlines():
        if "Duration:" not in line:
            continue
        stamp = line.split("Duration:", 1)[1].split(",", 1)[0].strip()
        parts = stamp.split(":")
        if len(parts) != 3:
            return None
        try:
            hours = float(parts[0])
            minutes = float(parts[1])
            seconds = float(parts[2])
        except ValueError:
            return None
        total = hours * 3600 + minutes * 60 + seconds
        if total <= 0:
            return None
        return total
    return None


def check_loop_file(path: str, runner=None) -> None:
    """Reject a loop that is missing or outside 10–30 seconds."""
    if not path or not path.strip():
        raise ValueError("video loop needs a file on this encoder")
    if not Path(path).is_file():
        raise ValueError("video loop file was not found")
    seconds = media_duration_seconds(path, runner)
    if seconds is None:
        return
    if not 10 <= seconds <= 30:
        raise ValueError("video loop must be 10 to 30 seconds")


class PcmRing:
    """One second of interleaved s16le stereo. The reader follows the write counter."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(PCM_MAGIC + struct.pack("<Q", 0) + b"\x00" * (PCM_CAP * 4))

    def write(self, pcm: bytes) -> None:
        payload = pcm[: len(pcm) - (len(pcm) % 4)]
        if not payload:
            return
        samples = len(payload) // 4
        with self.path.open("r+b") as handle:
            handle.seek(4)
            count = struct.unpack("<Q", handle.read(8))[0]
            offset = (count % PCM_CAP) * 4
            first = min(samples, PCM_CAP - (count % PCM_CAP))
            handle.seek(PCM_HEADER + offset)
            handle.write(payload[: first * 4])
            rest = samples - first
            if rest:
                handle.seek(PCM_HEADER)
                handle.write(payload[first * 4: first * 4 + rest * 4])
            handle.seek(4)
            handle.write(struct.pack("<Q", count + samples))
            handle.flush()


def _stop_proc(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()


def _read_exact(pipe, size: int, stop: threading.Event) -> bytes | None:
    buf = bytearray()
    while len(buf) < size and not stop.is_set():
        chunk = pipe.read(size - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    if len(buf) != size:
        return None
    return bytes(buf)


def run_video_pipe(argv: list[str], writer: SlotWriter, stop: threading.Event, spawn=subprocess.Popen) -> None:
    """Read raw BGRA from ffmpeg stdout into a slot until stop."""
    proc = None
    try:
        proc = spawn(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        frame = writer.nbytes
        while not stop.is_set():
            pixels = _read_exact(proc.stdout, frame, stop)
            if pixels is None:
                break
            writer.write(pixels)
    except Exception:
        log.warning("video pipe stopped", exc_info=True)
    finally:
        _stop_proc(proc)


def _run_srt_once(
    argv: list[str],
    video: SlotWriter,
    pcm: PcmRing,
    signal_file: Path,
    stop: threading.Event,
    spawn,
) -> None:
    from .effects import write_flag

    proc = None
    audio_fd = None
    source_file = signal_file.with_name(signal_file.stem + ".source.json")
    try:
        read_fd, write_fd = os.pipe()
        try:
            if write_fd != AUDIO_FD:
                os.dup2(write_fd, AUDIO_FD)
                os.close(write_fd)
                write_fd = AUDIO_FD
            proc = spawn(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=(AUDIO_FD,), close_fds=True,
            )
        finally:
            try:
                os.close(AUDIO_FD)
            except OSError:
                pass
        audio_fd = read_fd
        audio = os.fdopen(audio_fd, "rb")
        audio_fd = None
        if proc.stderr is not None:
            threading.Thread(
                target=_watch_srt_log,
                args=(proc.stderr, source_file, stop),
                name="nexencoder-srt-log",
                daemon=True,
            ).start()

        def pump_audio() -> None:
            while not stop.is_set():
                chunk = audio.read(4096)
                if not chunk:
                    break
                pcm.write(chunk)

        threading.Thread(target=pump_audio, name="nexencoder-pcm", daemon=True).start()
        frame = video.nbytes
        seen = False
        while not stop.is_set():
            pixels = _read_exact(proc.stdout, frame, stop)
            if pixels is None:
                break
            video.write(pixels)
            if not seen:
                write_flag(signal_file, True)
                seen = True
    except Exception:
        log.warning("SRT ingest stopped", exc_info=True)
    finally:
        write_flag(signal_file, False)
        source_file.unlink(missing_ok=True)
        if audio_fd is not None:
            os.close(audio_fd)
        _stop_proc(proc)


def run_srt_ingest(
    argv: list[str],
    video: SlotWriter,
    pcm: PcmRing,
    signal_file: Path,
    stop: threading.Event,
    spawn=subprocess.Popen,
) -> None:
    """Listen until stopped. A caller covers layer −5; a hangup brings it back."""
    if os.name != "posix":
        log.warning("SRT ingest runs on the encoder host")
        return
    while not stop.is_set():
        _run_srt_once(argv, video, pcm, signal_file, stop, spawn)
        if stop.wait(2):
            break


def _watch_srt_log(stream, source_file: Path, stop: threading.Event) -> None:
    """Keep the caller's raster from the ffmpeg stream banner."""
    from .detect import note_source_line

    try:
        for raw in stream:
            if stop.is_set():
                break
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            note_source_line(source_file, line)
    except Exception:
        log.warning("SRT format watcher stopped", exc_info=True)


def watch_decklink_signal(stream, signal_file: Path, stop: threading.Event) -> None:
    """Follow ffmpeg's signal-loss warnings onto the signal flag."""
    from .effects import write_flag

    try:
        for raw in stream:
            if stop.is_set():
                break
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            locked = signal_from_decklink_log(line)
            if locked is not None:
                write_flag(signal_file, locked)
                log.info("SDI signal %s", "locked" if locked else "lost")
    except Exception:
        log.warning("signal watcher stopped", exc_info=True)
