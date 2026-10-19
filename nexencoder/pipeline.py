"""FFmpeg and TSDuck argv for one HD-SDI input.

DeckLink opens exclusively, so one ffmpeg writes the program MPEG-TS.
TSDuck inserts SCTE-35 on that stream and publishes the local feed that
SRT and HLS read. The WebRTC proxy is a later process: it decodes that
same feed, so the monitor shows the picture SRT is sending. Argv builders
do not spawn processes.
"""

from __future__ import annotations

import ipaddress

from .effects import STILL_SECONDS, escape_filter_value
from .signal import SignalMode, h264_level, lavfi_rate, signal_mode

FEED_PORT = {"A": 19001, "B": 19002}
SPLICE_PORT = {"A": 19101, "B": 19102}
VANC_PORT = {"A": 19201, "B": 19202}
# TSDuck 3.45 ships the ip plugin and no udp plugin. The ip input only joins a
# multicast group, so the program feed is this group on the loopback interface.
FEED_ADDR = "239.255.1.1"
FEED_LOCAL = "127.0.0.1"
# Kernel receive buffer for each reader of the program feed. The default
# (~200 KB) drops a datagram when the monitor deinterlaces, and that hole
# pixelates until the next keyframe. About 3.5 s at 18 Mbps.
FEED_RCVBUF = 8 * 1024 * 1024
SCTE_PID = 0x1F0
PREVIEW_SIZE = "960:540"
# The capture process writes the clean still here. A file output makes
# FFmpeg exit when the JPEG already exists, which stops the program.
STILL_FD = 4
# The argv builders name child pipes by these numbers. The spawner replaces
# each ``pipe:N`` with the descriptor it really opened, so the parent never
# dup2()s onto a number another thread may be using.
PROGRESS_FD = 3


def bind_pipe_fds(argv: list[str], fds: dict[int, int]) -> list[str]:
    """Return argv with ``pipe:<placeholder>`` swapped for ``pipe:<real fd>``."""
    names = {f"pipe:{placeholder}": f"pipe:{real}" for placeholder, real in fds.items()}
    return [names.get(item, item) for item in argv]
PREVIEW_RTSP = "rtsp://127.0.0.1:8554"
# Program audio is AAC. The preview path uses Opus and is not this rate.
AUDIO_BITRATE = "192k"
AUDIO_BITRATE_BPS = 192_000
AUDIO_CHANNELS = 2
AUDIO_RATE_HZ = 48000
SRT_LATENCY_MS = 1500
# TSDuck treats this as a minimum. Stats are logged only while packets flow.
SRT_STATS_INTERVAL_MS = 5000
SRT_STATS_PREFIX = "nexencoder-srt"
HLS_SEGMENT_SECONDS = 2


def preview_path(channel_id: str) -> str:
    return f"input{channel_id}"


def preview_rtsp(channel_id: str) -> str:
    return f"{PREVIEW_RTSP}/{preview_path(channel_id)}"


def feed_url(channel_id: str) -> str:
    """Multicast program feed. SRT, HLS, and the monitor each join it.

    ``buffer_size`` is the kernel socket buffer. ``fifo_size`` is FFmpeg's
    own queue and does not stop the kernel from dropping datagrams first.
    """
    feed = FEED_PORT[channel_id]
    return (
        f"udp://{FEED_ADDR}:{feed}?localaddr={FEED_LOCAL}"
        f"&reuse=1&fifo_size=2000000&overrun_nonfatal=1&buffer_size={FEED_RCVBUF}"
    )


def whep_url(channel_id: str, host: str = "127.0.0.1", port: int = 8889) -> str:
    shown = host
    try:
        if isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address):
            shown = f"[{host}]"
    except ValueError:
        pass
    return f"http://{shown}:{port}/{preview_path(channel_id)}/whep"


def _x264_params(mode: SignalMode) -> str:
    """Slice threads keep zerolatency on more than one core.

    Parameter sets on every IDR let the monitor join without waiting for
    the next random-access point.
    """
    parts = ["sliced-threads=1", "repeat-headers=1"]
    if mode.field == "tff":
        parts.insert(0, "tff=1")
    elif mode.field == "bff":
        parts.insert(0, "bff=1")
    return ":".join(parts)


def _video_args(mode: SignalMode, bitrate_mbps: int) -> list[str]:
    level = h264_level(mode)
    args = [
        "-c:v", "libx264",
        "-a53cc", "1",
        "-preset", "veryfast",
        "-tune", "zerolatency",
        "-profile:v", "high",
        "-level", level,
        "-pix_fmt", "yuv420p",
        "-g", "30" if mode.interlaced else "60",
        "-b:v", f"{bitrate_mbps}M",
        "-maxrate", f"{bitrate_mbps}M",
        "-bufsize", f"{bitrate_mbps * 2}M",
        "-x264-params", _x264_params(mode),
    ]
    if mode.field in ("tff", "bff"):
        args += ["-flags", "+ildct+ilme"]
    args += ["-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", str(AUDIO_RATE_HZ), "-ac", str(AUDIO_CHANNELS)]
    return args


def _tone_filter(hold_path: str, pcm_path: str | None) -> str:
    hold = escape_filter_value(hold_path)
    tone = f"nextone=hold={hold}"
    if pcm_path:
        tone += f":pcm={escape_filter_value(pcm_path)}"
    return (
        "aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=stereo,"
        f"{tone}"
    )


def _preview_filter() -> str:
    """Display size for the monitor. The program raster is unchanged."""
    return (
        f"yadif=mode=0:parity=-1:deint=interlaced,"
        f"scale={PREVIEW_SIZE}:force_original_aspect_ratio=decrease,"
        f"fps=30,format=yuv420p"
    )


def _filter_graph(interlaced: bool, effect_state: str, hold_path: str, pcm_path: str | None = None, audio_pad: str = "0:a") -> str:
    """Squeeze and CG run on the program frame.

    The still is a second branch of the input frame, taken before the
    effect, so the builder can place corners on the clean picture. A/53
    side data stays on the program frame: ``nexeffect`` copies it after the
    stack is flattened. SCTE-104 is forwarded by the demuxer and never
    enters this graph. ``nextone`` swaps in a tone while color bars are up.
    The monitor is not a branch of this graph.
    """
    state = escape_filter_value(effect_state)
    hold = escape_filter_value(hold_path)
    field = 1 if interlaced else 0
    return (
        "[0:v]split=2[vsrc][vstill];"
        f"[vsrc]nexeffect=state={state}:interlaced={field}:hold={hold}[vprog];"
        f"[vstill]fps=1/{STILL_SECONDS},"
        f"scale={PREVIEW_SIZE}:force_original_aspect_ratio=decrease,format=yuv420p[vstillout];"
        f"[{audio_pad}]{_tone_filter(hold_path, pcm_path)}[aprog]"
    )


def capture_argv(
    channel_id: str,
    device: str,
    format_name: str,
    bitrate_mbps: int,
    ffmpeg: str = "ffmpeg",
    progress_path: str | None = None,
    effect_state: str | None = None,
    still_path: str | None = None,
    hold_path: str | None = None,
    pcm_path: str | None = None,
) -> list[str]:
    """One DeckLink open: H.264/AAC MPEG-TS on stdout.

    yuv422p10 keeps the VANC lines so CEA-708 CDPs become A/53 side data.
    The clean still is an MJPEG stream on fd 4. The channel process writes
    the JPEG, so a failure to replace that file does not stop the encode.
    The monitor publishes from the program feed, not from this process.
    """
    mode = signal_mode(format_name)
    progress = ["-progress", progress_path] if progress_path else []
    state = effect_state or f"/var/lib/nexencoder/state/{channel_id}.effect"
    hold = hold_path or f"/var/lib/nexencoder/state/{channel_id}.wait"
    del still_path  # The parent writes the JPEG. This process only emits the pipe.
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning",
        *progress,
        "-f", "decklink",
        "-raw_format", "yuv422p10",
        "-audio_input", "embedded",
        # bars paints 8-bit only, and this capture is yuv422p10. repeat keeps
        # the last picture when the input drops so the encode stays up; the
        # waiting picture is drawn by nexeffect from the signal flag.
        "-signal_loss_action", "repeat",
        "-format_code", mode.format_code,
        "-i", device,
        "-filter_complex", _filter_graph(mode.interlaced, state, hold, pcm_path),
        "-map", "[vprog]", "-map", "[aprog]",
        *_video_args(mode, bitrate_mbps),
        "-f", "mpegts",
        "-mpegts_flags", "+resend_headers",
        "pipe:1",
        "-map", "[vstillout]", "-an",
        "-c:v", "mjpeg", "-q:v", "5",
        "-f", "image2pipe",
        f"pipe:{STILL_FD}",
    ]


def clock_argv(
    channel_id: str,
    format_name: str,
    bitrate_mbps: int,
    ffmpeg: str = "ffmpeg",
    progress_path: str | None = None,
    effect_state: str | None = None,
    hold_path: str | None = None,
    pcm_path: str | None = None,
) -> list[str]:
    """Always-on encode for an SRT input. The picture is black until a caller connects.

    The SRT listener runs in its own process and writes a live slot. This
    process does not open that listener, so a missing caller cannot stall
    the output.
    """
    mode = signal_mode(format_name)
    progress = ["-progress", progress_path] if progress_path else []
    state = effect_state or f"/var/lib/nexencoder/state/{channel_id}.effect"
    hold = hold_path or f"/var/lib/nexencoder/state/{channel_id}.wait"
    color = f"color=c=black:s={mode.width}x{mode.height}:r={lavfi_rate(mode)}"
    field = 1 if mode.interlaced else 0
    graph = (
        f"[0:v]nexeffect=state={escape_filter_value(state)}:interlaced={field}:hold={escape_filter_value(hold)}[vprog];"
        f"[1:a]{_tone_filter(hold, pcm_path)}[aprog]"
    )
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning",
        *progress,
        "-f", "lavfi", "-i", color,
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-filter_complex", graph,
        "-map", "[vprog]", "-map", "[aprog]",
        *_video_args(mode, bitrate_mbps),
        "-f", "mpegts",
        "-mpegts_flags", "+resend_headers",
        "pipe:1",
    ]


def preview_argv(channel_id: str, ffmpeg: str = "ffmpeg", source: str | None = None) -> list[str]:
    """Decode the program feed and publish the monitor.

    Squeeze, graphics, bars, and the ident are already in this MPEG-TS,
    which is the stream SRT sends. Restarting this process does not
    restart the program encode. The channel passes ``pipe:0`` and writes
    that pipe only from the first video keyframe, so the decoder is not
    handed slices that have no parameter sets.
    """
    src = feed_url(channel_id) if source is None else source
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning",
        "-fflags", "+genpts+discardcorrupt",
        # The pipe already starts on a keyframe. Do not sit for the default
        # five-second probe before the RTSP session opens.
        "-analyzeduration", "1000000", "-probesize", "1000000",
        "-f", "mpegts", "-i", src,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", _preview_filter(),
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-g", "30", "-b:v", "1500k",
        "-c:a", "libopus", "-b:a", "96k", "-ar", "48000", "-ac", "2", "-application", "lowdelay",
        "-f", "rtsp", "-rtsp_transport", "tcp",
        # MediaMTX remuxes anything over 1440 bytes and the preview was sending 1460.
        "-pkt_size", "1316",
        preview_rtsp(channel_id),
    ]


def splice_argv(channel_id: str, tsp: str = "tsp") -> list[str]:
    """Read MPEG-TS on stdin, declare the SCTE-35 PID, inject, write the local feed."""
    port = FEED_PORT[channel_id]
    splice = SPLICE_PORT[channel_id]
    return [
        tsp,
        "--add-input-stuffing", "1/8",
        "-I", "file", "-",
        "-P", "pmt",
        "--service", "-",
        "--add-programinfo-id", "0x43554549",
        "--add-pid", f"{SCTE_PID}/0x86",
        "-P", "spliceinject",
        "--service", "-",
        "--pid", str(SCTE_PID),
        "--udp", f"127.0.0.1:{splice}",
        "--inject-count", "3",
        "--inject-interval", "500",
        "--min-inter-packet", "400",
        "-P", "filter", "--negate", "--pid", "0x1FFF",
        "-O", "ip", f"{FEED_ADDR}:{port}",
        "--local-address", FEED_LOCAL,
    ]


def srt_argv(
    channel_id: str,
    mode: str,
    host: str,
    port: int,
    tsp: str = "tsp",
    statistics: bool = True,
) -> list[str]:
    """SRT egress via TSDuck. GStreamer is not on this path.

    Statistics flags are omitted when the installed tsp has no ``--json-line``
    support. The channel supervisor decides that; the builder itself stays pure.
    """
    feed = FEED_PORT[channel_id]
    argv = [
        tsp,
        "-I", "ip", f"{FEED_ADDR}:{feed}",
        "--local-address", FEED_LOCAL,
        "-O", "srt",
        "--latency", str(SRT_LATENCY_MS),
        "--transtype", "live",
    ]
    if mode == "srt-listener":
        argv += ["--listener", f":{int(port)}", "--multiple"]
    elif mode == "srt-caller":
        if not host:
            raise ValueError("SRT caller needs a listener address")
        argv += ["--caller", f"{host}:{int(port)}"]
    else:
        raise ValueError(f"{mode} is not an SRT delivery")
    if statistics:
        argv += [
            "--statistics-interval", str(SRT_STATS_INTERVAL_MS),
            f"--json-line={SRT_STATS_PREFIX}",
        ]
    return argv


def hls_argv(
    channel_id: str,
    mode: str,
    url: str,
    publish_dir: str,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    src = feed_url(channel_id)
    argv = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning",
        "-f", "mpegts", "-i", src,
        "-map", "0", "-c", "copy", "-copyts",
        "-f", "hls",
        "-hls_time", str(HLS_SEGMENT_SECONDS),
        "-hls_list_size", "10",
        "-hls_flags", "delete_segments+append_list+independent_segments+program_date_time",
        "-hls_segment_type", "mpegts",
    ]
    if mode == "hls-pull":
        argv += [
            "-hls_segment_filename", f"{publish_dir}/seg_%05d.ts",
            f"{publish_dir}/index.m3u8",
        ]
    elif mode == "hls-push":
        if not url:
            raise ValueError("HLS push needs a URL")
        argv += ["-method", "PUT", url]
    else:
        raise ValueError(f"{mode} is not an HLS delivery")
    return argv


def sections_from_mom(payload: bytes, frame_pts: int = 0) -> list[bytes]:
    """Turn one SCTE-104 message into the SCTE-35 sections spliceinject expects."""
    from .scte104 import parse_mom
    from .translate import translate_mom

    sections = translate_mom(parse_mom(payload), frame_pts)
    if not sections:
        raise ValueError("SCTE-104 message has no splice to insert")
    return sections


def egress_argv(
    channel_id: str,
    mode: str,
    host: str,
    port: int,
    url: str,
    publish_dir: str,
    ffmpeg: str = "ffmpeg",
    tsp: str = "tsp",
    srt_statistics: bool = True,
) -> list[str]:
    if mode.startswith("srt-"):
        return srt_argv(channel_id, mode, host, port, tsp, statistics=srt_statistics)
    return hls_argv(channel_id, mode, url, publish_dir, ffmpeg)
