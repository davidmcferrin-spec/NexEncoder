"""FFmpeg and TSDuck argv for one HD-SDI input.

DeckLink opens exclusively, so the program encode and the WebRTC proxy are
two outputs of one ffmpeg process. TSDuck inserts SCTE-35 on the MPEG-TS
that ffmpeg writes to its stdout. Argv builders do not spawn processes.
"""

from __future__ import annotations

from .signal import SignalMode, h264_level, signal_mode

FEED_PORT = {"A": 19001, "B": 19002}
SPLICE_PORT = {"A": 19101, "B": 19102}
SCTE_PID = 0x1F0
PREVIEW_SIZE = "960:540"
PREVIEW_RTSP = "rtsp://127.0.0.1:8554"


def preview_path(channel_id: str) -> str:
    return f"input{channel_id}"


def preview_rtsp(channel_id: str) -> str:
    return f"{PREVIEW_RTSP}/{preview_path(channel_id)}"


def whep_url(channel_id: str, host: str = "127.0.0.1", port: int = 8889) -> str:
    return f"http://{host}:{port}/{preview_path(channel_id)}/whep"


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
    ]
    if mode.field == "tff":
        args += ["-flags", "+ildct+ilme", "-x264-params", "tff=1"]
    elif mode.field == "bff":
        args += ["-flags", "+ildct+ilme", "-x264-params", "bff=1"]
    args += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    return args


def _filter_graph() -> str:
    preview = (
        f"yadif=mode=0:parity=-1:deint=interlaced,"
        f"scale={PREVIEW_SIZE}:force_original_aspect_ratio=decrease,"
        f"fps=30,format=yuv420p"
    )
    return (
        "[0:v]split=2[vprog][vp0];"
        f"[vp0]{preview}[vprev];"
        "[0:a]asplit=2[aprog][aprev]"
    )


def capture_argv(
    channel_id: str,
    device: str,
    format_name: str,
    bitrate_mbps: int,
    ffmpeg: str = "ffmpeg",
    progress_path: str | None = None,
) -> list[str]:
    """One DeckLink open: H.264/AAC MPEG-TS on stdout, proxy RTSP beside it.

    yuv422p10 keeps the VANC lines so CEA-708 CDPs become A/53 side data.
    """
    mode = signal_mode(format_name)
    progress = ["-progress", progress_path] if progress_path else []
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning",
        *progress,
        "-f", "decklink",
        "-raw_format", "yuv422p10",
        "-audio_input", "embedded",
        "-format_code", mode.format_code,
        "-i", device,
        "-filter_complex", _filter_graph(),
        "-map", "[vprog]", "-map", "[aprog]",
        *_video_args(mode, bitrate_mbps),
        "-f", "mpegts",
        "-mpegts_flags", "+resend_headers",
        "pipe:1",
        "-map", "[vprev]", "-map", "[aprev]",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-g", "30", "-b:v", "1500k",
        "-c:a", "libopus", "-b:a", "96k", "-ar", "48000", "-ac", "2", "-application", "lowdelay",
        "-f", "rtsp", "-rtsp_transport", "tcp",
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
        "-O", "ip", f"239.255.98.1:{port}",
        "--local-address", "127.0.0.1",
    ]


def srt_argv(channel_id: str, mode: str, host: str, port: int, tsp: str = "tsp") -> list[str]:
    feed = FEED_PORT[channel_id]
    argv = [
        tsp,
        "-I", "ip", f"239.255.98.1:{feed}",
        "--local-address", "127.0.0.1",
        "-O", "srt",
        "--latency", "1500",
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
    return argv


def hls_argv(
    channel_id: str,
    mode: str,
    url: str,
    publish_dir: str,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    feed = FEED_PORT[channel_id]
    src = f"udp://239.255.98.1:{feed}?localaddr=127.0.0.1&reuse=1&fifo_size=2000000&overrun_nonfatal=1"
    argv = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning",
        "-f", "mpegts", "-i", src,
        "-map", "0", "-c", "copy", "-copyts",
        "-f", "hls",
        "-hls_time", "2",
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
) -> list[str]:
    if mode.startswith("srt-"):
        return srt_argv(channel_id, mode, host, port, tsp)
    return hls_argv(channel_id, mode, url, publish_dir, ffmpeg)
