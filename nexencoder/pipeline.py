"""FFmpeg and TSDuck argv for one HD-SDI input.

DeckLink opens exclusively, so the program encode and the WebRTC proxy are
two outputs of one ffmpeg process. TSDuck inserts SCTE-35 on the MPEG-TS
that ffmpeg writes to its stdout. Argv builders do not spawn processes.
"""

from __future__ import annotations

from .effects import STILL_SECONDS, escape_filter_value
from .signal import SignalMode, h264_level, lavfi_rate, signal_mode

FEED_PORT = {"A": 19001, "B": 19002}
SPLICE_PORT = {"A": 19101, "B": 19102}
VANC_PORT = {"A": 19201, "B": 19202}
# One reader per channel, so the program feed stays on loopback unicast.
FEED_ADDR = "127.0.0.1"
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


def _tone_filter(hold_path: str, pcm_path: str | None) -> str:
    hold = escape_filter_value(hold_path)
    tone = f"nextone=hold={hold}"
    if pcm_path:
        tone += f":pcm={escape_filter_value(pcm_path)}"
    return (
        "aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=stereo,"
        f"{tone}"
    )


def _filter_graph(interlaced: bool, effect_state: str, hold_path: str, pcm_path: str | None = None, audio_pad: str = "0:a") -> str:
    """Squeeze and CG run on the program before the preview split.

    The still is a second branch of the input frame, taken before the
    effect, so the builder can place corners on the clean picture. A/53
    side data stays on the program frame: ``nexeffect`` copies it after the
    stack is flattened. SCTE-104 is forwarded by the demuxer and never
    enters this graph. ``nextone`` swaps in a tone while color bars are up.
    """
    preview = (
        f"yadif=mode=0:parity=-1:deint=interlaced,"
        f"scale={PREVIEW_SIZE}:force_original_aspect_ratio=decrease,"
        f"fps=30,format=yuv420p"
    )
    state = escape_filter_value(effect_state)
    hold = escape_filter_value(hold_path)
    field = 1 if interlaced else 0
    return (
        "[0:v]split=2[vsrc][vstill];"
        f"[vsrc]nexeffect=state={state}:interlaced={field}:hold={hold}[vfx];"
        "[vfx]split=2[vprog][vp0];"
        f"[vp0]{preview}[vprev];"
        f"[vstill]fps=1/{STILL_SECONDS},"
        f"scale={PREVIEW_SIZE}:force_original_aspect_ratio=decrease,format=yuv420p[vstillout];"
        f"[{audio_pad}]{_tone_filter(hold_path, pcm_path)}[atone];"
        "[atone]asplit=2[aprog][aprev]"
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
    """One DeckLink open: H.264/AAC MPEG-TS on stdout, proxy RTSP beside it.

    yuv422p10 keeps the VANC lines so CEA-708 CDPs become A/53 side data.
    The still JPEG is rewritten every few seconds for the effect builder.
    """
    mode = signal_mode(format_name)
    progress = ["-progress", progress_path] if progress_path else []
    state = effect_state or f"/var/lib/nexencoder/state/{channel_id}.effect"
    still = still_path or f"/var/lib/nexencoder/state/{channel_id}.jpg"
    hold = hold_path or f"/var/lib/nexencoder/state/{channel_id}.wait"
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning",
        *progress,
        "-f", "decklink",
        "-raw_format", "yuv422p10",
        "-audio_input", "embedded",
        "-signal_loss_action", "none",
        "-format_code", mode.format_code,
        "-i", device,
        "-filter_complex", _filter_graph(mode.interlaced, state, hold, pcm_path),
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
        "-map", "[vstillout]", "-an",
        "-c:v", "mjpeg", "-q:v", "5",
        "-f", "image2", "-update", "1", "-atomic_writing", "1",
        still,
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
    preview = (
        f"yadif=mode=0:parity=-1:deint=interlaced,"
        f"scale={PREVIEW_SIZE}:force_original_aspect_ratio=decrease,"
        f"fps=30,format=yuv420p"
    )
    graph = (
        f"[0:v]nexeffect=state={escape_filter_value(state)}:interlaced={field}:hold={escape_filter_value(hold)}[vfx];"
        "[vfx]split=2[vprog][vp0];"
        f"[vp0]{preview}[vprev];"
        f"[1:a]{_tone_filter(hold, pcm_path)}[atone];"
        "[atone]asplit=2[aprog][aprev]"
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
        "-O", "ip", f"{FEED_ADDR}:{port}",
        "--local-address", FEED_ADDR,
    ]


def srt_argv(channel_id: str, mode: str, host: str, port: int, tsp: str = "tsp") -> list[str]:
    feed = FEED_PORT[channel_id]
    argv = [
        tsp,
        "-I", "ip", f"{FEED_ADDR}:{feed}",
        "--local-address", FEED_ADDR,
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
    src = f"udp://{FEED_ADDR}:{feed}?localaddr={FEED_ADDR}&reuse=1&fifo_size=2000000&overrun_nonfatal=1"
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
