"""HD-SDI rasters the encoder accepts, and the FFmpeg DeckLink format codes for them."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SignalMode:
    format_name: str
    format_code: str
    width: int
    height: int
    field_rate_hz: float
    frame_rate_hz: float
    interlaced: bool
    field: str | None


# 1080i names use the field rate. FFmpeg's format_code uses the frame rate.
MODES: dict[str, SignalMode] = {
    "1080i59.94": SignalMode("1080i59.94", "Hi59", 1920, 1080, 59.94, 30000 / 1001, True, "tff"),
    "1080i50": SignalMode("1080i50", "Hi50", 1920, 1080, 50.0, 25.0, True, "tff"),
    "1080p59.94": SignalMode("1080p59.94", "Hp59", 1920, 1080, 60000 / 1001, 60000 / 1001, False, None),
    "1080p60": SignalMode("1080p60", "Hp60", 1920, 1080, 60.0, 60.0, False, None),
    "1080p50": SignalMode("1080p50", "Hp50", 1920, 1080, 50.0, 50.0, False, None),
    "720p59.94": SignalMode("720p59.94", "hp59", 1280, 720, 60000 / 1001, 60000 / 1001, False, None),
    "720p50": SignalMode("720p50", "hp50", 1280, 720, 50.0, 50.0, False, None),
}

LAVFI_RATES = {
    "1080i59.94": "30000/1001",
    "1080i50": "25",
    "1080p59.94": "60000/1001",
    "1080p60": "60",
    "1080p50": "50",
    "720p59.94": "60000/1001",
    "720p50": "50",
}


def lavfi_rate(mode: SignalMode) -> str:
    """Exact lavfi frame rate. 1080i counts frames, not fields."""
    try:
        return LAVFI_RATES[mode.format_name]
    except KeyError as exc:
        raise ValueError(f"unsupported raster {mode.format_name!r}") from exc


def signal_mode(format_name: str) -> SignalMode:
    try:
        return MODES[format_name]
    except KeyError as exc:
        raise ValueError(f"unsupported raster {format_name!r}") from exc


def h264_level(mode: SignalMode) -> str:
    """Lowest level that covers the raster. 1080i59.94 and 720p59.94 fit in 4.2."""
    mb = ((mode.width + 15) // 16) * ((mode.height + 15) // 16)
    rate = mb * mode.frame_rate_hz
    for level, mbps, max_fs in (("4.1", 245760, 8192), ("4.2", 522240, 8704), ("5.1", 983040, 36864)):
        if mb <= max_fs and rate <= mbps:
            return level
    return "5.1"
