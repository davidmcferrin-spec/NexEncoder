"""Prometheus text exposition for the encoder.

Measured bitrate, resolution, and round-trip are omitted until a pipeline
reports them. Zeros are reserved for counters and for signals that are
actually down.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import __version__


@dataclass(frozen=True)
class InputStatus:
    input_id: str
    up: bool = False
    format_name: str = "1080i59.94"
    scan: str = "interlaced"
    audio: str = "unlocked"
    width: int | None = None
    height: int | None = None
    field_rate_hz: float | None = None
    captions_up: bool = False
    scte104_messages: int = 0


@dataclass(frozen=True)
class OutputStatus:
    output_id: str
    up: bool = False
    connected: bool = False
    mode: str = "srt-listener"
    video_bitrate_bps: float | None = None
    video_bitrate_target_bps: float | None = None
    width: int | None = None
    height: int | None = None
    dropped_frames: int = 0
    rtt_seconds: float | None = None
    packets_lost: int | None = None
    slot: str = "1"


@dataclass(frozen=True)
class Snapshot:
    inputs: tuple[InputStatus, ...]
    outputs: tuple[OutputStatus, ...]


def default_snapshot() -> Snapshot:
    return Snapshot(
        inputs=(InputStatus("A"), InputStatus("B")),
        outputs=(
            OutputStatus("A"),
            OutputStatus("B", mode="srt-listener"),
        ),
    )


# Cards the metrics page always shows. Host queries need Prometheus.
# Encoder queries also render from the local /metrics poll.
CATALOG: tuple[dict[str, object], ...] = (
    {
        "id": "cpu",
        "group": "host",
        "title": "CPU",
        "unit": "%",
        "kind": "percent",
        "query": '100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[1m])))',
    },
    {
        "id": "memory",
        "group": "host",
        "title": "Memory",
        "unit": "%",
        "kind": "percent",
        "query": "100 * (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))",
    },
    {
        "id": "disk",
        "group": "host",
        "title": "Hard drive",
        "unit": "%",
        "kind": "percent",
        "query": (
            '100 * (1 - (node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay|squashfs"}'
            ' / node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay|squashfs"}))'
        ),
    },
    {
        "id": "uptime",
        "group": "host",
        "title": "Uptime",
        "unit": "s",
        "kind": "duration",
        "query": "node_time_seconds - node_boot_time_seconds",
    },
    {
        "id": "gpu",
        "group": "host",
        "title": "GPU",
        "unit": "%",
        "kind": "percent",
        "queries": (
            "avg(nvidia_smi_utilization_gpu_ratio) * 100",
            "avg(DCGM_FI_DEV_GPU_UTIL)",
        ),
    },
    {"id": "up", "group": "encoder", "title": "Process", "unit": "", "kind": "gauge", "query": "nexencoder_up"},
    {"id": "input-up", "group": "encoder", "title": "Input signal", "unit": "", "kind": "gauge", "query": "nexencoder_input_up"},
    {"id": "input-info", "group": "encoder", "title": "Input format", "unit": "", "kind": "info", "query": "nexencoder_input_info"},
    {"id": "captions", "group": "encoder", "title": "Captions", "unit": "", "kind": "gauge", "query": "nexencoder_input_captions_up"},
    {"id": "scte", "group": "encoder", "title": "SCTE-104 messages", "unit": "", "kind": "counter", "query": "nexencoder_input_scte104_messages_total"},
    {"id": "output-up", "group": "encoder", "title": "Output pipeline", "unit": "", "kind": "gauge", "query": "nexencoder_output_up"},
    {"id": "output-connected", "group": "encoder", "title": "Output connected", "unit": "", "kind": "gauge", "query": "nexencoder_output_connected"},
    {"id": "output-info", "group": "encoder", "title": "Delivery", "unit": "", "kind": "info", "query": "nexencoder_output_info"},
    {"id": "bitrate", "group": "encoder", "title": "Video bitrate", "unit": "bps", "kind": "gauge", "query": "nexencoder_output_video_bitrate_bps"},
    {"id": "bitrate-target", "group": "encoder", "title": "Target bitrate", "unit": "bps", "kind": "gauge", "query": "nexencoder_output_video_bitrate_target_bps"},
    {"id": "drops", "group": "encoder", "title": "Dropped frames", "unit": "", "kind": "counter", "query": "nexencoder_output_dropped_frames_total"},
    {"id": "rtt", "group": "encoder", "title": "SRT round-trip", "unit": "s", "kind": "gauge", "query": "nexencoder_output_rtt_seconds"},
    {"id": "loss", "group": "encoder", "title": "SRT packets lost", "unit": "", "kind": "counter", "query": "nexencoder_output_packets_lost_total"},
)


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _label_num(value: float) -> str:
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _raster_labels(width: int | None, height: int | None, field_rate_hz: float | None) -> dict[str, str]:
    """One locked raster. These are not time series."""
    labels: dict[str, str] = {}
    if width is not None and height is not None:
        labels["raster"] = f"{int(width)}x{int(height)}"
    if field_rate_hz is not None:
        labels["field_rate_hz"] = _label_num(field_rate_hz)
    return labels


def _num(value: float) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text or "0"


class _Writer:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def header(self, name: str, typ: str, help_text: str) -> None:
        self.lines.append(f"# HELP {name} {help_text}")
        self.lines.append(f"# TYPE {name} {typ}")

    def sample(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        if labels:
            body = ",".join(f'{key}="{_esc(val)}"' for key, val in labels.items())
            self.lines.append(f"{name}{{{body}}} {_num(value)}")
        else:
            self.lines.append(f"{name} {_num(value)}")

    def optional(self, name: str, value: float | None, labels: dict[str, str]) -> None:
        if value is not None:
            self.sample(name, value, labels)


def render_metrics(snapshot: Snapshot | None = None) -> str:
    snap = snapshot or default_snapshot()
    out = _Writer()
    out.header("nexencoder_up", "gauge", "1 when the console metrics endpoint is serving.")
    out.sample("nexencoder_up", 1)
    out.header("nexencoder_build_info", "gauge", "Build version. Value is always 1.")
    out.sample("nexencoder_build_info", 1, {"version": __version__})

    out.header("nexencoder_input_up", "gauge", "1 when the SDI input has a signal.")
    out.header("nexencoder_input_info", "gauge", "Raster and audio lock. Value is always 1.")
    out.header("nexencoder_input_width", "gauge", "Active video width in pixels.")
    out.header("nexencoder_input_height", "gauge", "Active video height in pixels.")
    out.header("nexencoder_input_field_rate_hz", "gauge", "Field rate in hertz.")
    out.header("nexencoder_input_captions_up", "gauge", "1 when CEA-608 or CEA-708 was seen recently.")
    out.header("nexencoder_input_scte104_messages_total", "counter", "SCTE-104 messages received.")
    for item in snap.inputs:
        labels = {"input": item.input_id}
        out.sample("nexencoder_input_up", 1 if item.up else 0, labels)
        out.sample("nexencoder_input_info", 1, {
            "input": item.input_id,
            "format": item.format_name,
            "scan": item.scan,
            "audio": item.audio,
            **_raster_labels(item.width, item.height, item.field_rate_hz),
        })
        out.optional("nexencoder_input_width", item.width, labels)
        out.optional("nexencoder_input_height", item.height, labels)
        out.optional("nexencoder_input_field_rate_hz", item.field_rate_hz, labels)
        out.sample("nexencoder_input_captions_up", 1 if item.captions_up else 0, labels)
        out.sample("nexencoder_input_scte104_messages_total", item.scte104_messages, labels)

    out.header("nexencoder_output_up", "gauge", "1 when the encode pipeline is running.")
    out.header("nexencoder_output_connected", "gauge", "1 when this destination's SRT peer or HLS target is connected.")
    out.header("nexencoder_output_info", "gauge", "Delivery mode for one destination. Value is always 1.")
    out.header("nexencoder_output_video_bitrate_bps", "gauge", "Measured video bitrate in bits per second.")
    out.header("nexencoder_output_video_bitrate_target_bps", "gauge", "Configured video bitrate in bits per second.")
    out.header("nexencoder_output_width", "gauge", "Encoded width in pixels.")
    out.header("nexencoder_output_height", "gauge", "Encoded height in pixels.")
    out.header("nexencoder_output_dropped_frames_total", "counter", "Frames dropped by the encode pipeline.")
    out.header("nexencoder_output_rtt_seconds", "gauge", "SRT round-trip time in seconds.")
    out.header("nexencoder_output_packets_lost_total", "counter", "SRT packets lost.")
    seen_encode: set[str] = set()
    for item in snap.outputs:
        encode = {"output": item.output_id}
        dest = {"output": item.output_id, "destination": item.slot}
        if item.output_id not in seen_encode:
            seen_encode.add(item.output_id)
            out.sample("nexencoder_output_up", 1 if item.up else 0, encode)
            out.optional("nexencoder_output_video_bitrate_bps", item.video_bitrate_bps, encode)
            out.optional("nexencoder_output_video_bitrate_target_bps", item.video_bitrate_target_bps, encode)
            out.optional("nexencoder_output_width", item.width, encode)
            out.optional("nexencoder_output_height", item.height, encode)
            out.sample("nexencoder_output_dropped_frames_total", item.dropped_frames, encode)
        out.sample("nexencoder_output_connected", 1 if item.connected else 0, dest)
        out.sample("nexencoder_output_info", 1, {
            "output": item.output_id,
            "destination": item.slot,
            "mode": item.mode,
            **_raster_labels(item.width, item.height, None),
        })
        out.optional("nexencoder_output_rtt_seconds", item.rtt_seconds, dest)
        out.optional("nexencoder_output_packets_lost_total", item.packets_lost, dest)

    out.lines.append("")
    return "\n".join(out.lines)
