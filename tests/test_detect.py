"""Raster detection from DeckLink and SRT banners. No devices are opened."""

import json

from nexencoder.detect import detect_channel, note_source_line, parse_probe, raster_from_geometry
from nexencoder.effects import signal_path, write_flag


def test_geometry_maps_the_supported_rasters():
    assert raster_from_geometry(1920, 1080, 29.97, True) == "1080i59.94"
    assert raster_from_geometry(1920, 1080, 25, True) == "1080i50"
    assert raster_from_geometry(1920, 1080, 29.97, False) == "1080p29.97"
    assert raster_from_geometry(1920, 1080, 30, False) is None
    assert raster_from_geometry(1920, 1080, 59.94, False) == "1080p59.94"
    assert raster_from_geometry(1920, 1080, 60, False) == "1080p60"
    assert raster_from_geometry(1920, 1080, 50, False) == "1080p50"
    assert raster_from_geometry(1280, 720, 59.94, False) == "720p59.94"
    assert raster_from_geometry(1280, 720, 50, False) == "720p50"
    assert raster_from_geometry(1920, 1080, 23.98, False) is None
    assert raster_from_geometry(3840, 2160, 59.94, False) is None


def test_decklink_banner_and_status_json():
    found = parse_probe("Found Decklink mode 1920 x 1080 with rate 29.97(i)\n", "DeckLink Duo (1)")
    assert found["ok"] is True and found["format"] == "1080i59.94"
    progressive_slow = parse_probe("Found Decklink mode 1920 x 1080 with rate 29.97\n", "DeckLink Duo (1)")
    assert progressive_slow["ok"] is True and progressive_slow["format"] == "1080p29.97"
    progressive = parse_probe(
        "Stream #0:0: Video: rawvideo, uyvy422, 1920x1080, 50 fps, 50 tbr\n",
        "DeckLink Duo (1)",
    )
    assert progressive["format"] == "1080p50"
    quiet = parse_probe("No input signal detected\n", "DeckLink Duo (1)")
    assert quiet["ok"] is False and quiet["format"] == ""
    busy = parse_probe("[decklink] Device or resource busy\n", "DeckLink Duo (1)")
    assert "in use" in busy["detail"]
    odd = parse_probe("Found Decklink mode 1920 x 1080 with rate 23.98\n", "DeckLink Duo (1)")
    assert odd["ok"] is False and "not a supported raster" in odd["detail"]
    status = parse_probe(
        json.dumps({"devices": [{"name": "DeckLink Duo (2)", "input_locked": True, "input_mode": "720p50"}]}),
        "DeckLink Duo (2)",
    )
    assert status["format"] == "720p50"
    integer = parse_probe(
        json.dumps({"devices": [{"name": "DeckLink Duo (1)", "input_locked": True, "input_mode": "1080p60"}]}),
        "DeckLink Duo (1)",
    )
    assert integer["ok"] is True and integer["format"] == "1080p60"


def test_srt_caller_note_is_what_auto_saves(tmp_path):
    config = tmp_path / "config.json"
    state = tmp_path / "state"
    state.mkdir()
    line = "Stream #0:0: Video: h264 (High), yuv420p(tv, top coded first (swapped)), 1920x1080, 29.97 fps, 29.97 tbr\n"
    note_source_line(state / "B.source.json", line)
    saved = json.loads((state / "B.source.json").read_text(encoding="utf-8"))
    assert saved["format"] == "1080i59.94"
    write_flag(signal_path(state, "B"), True)
    config.write_text(json.dumps({
        "inputs": {"B": {"source": "srt", "format": "720p59.94"}},
    }), encoding="utf-8")

    def probe(_argv):
        raise AssertionError("a connected caller is read from the ingest note")

    found = detect_channel("B", config, state, probe=probe)
    assert found["format"] == "1080i59.94"
    assert json.loads(config.read_text(encoding="utf-8"))["inputs"]["B"]["format"] == "1080i59.94"


def test_idle_srt_listener_is_probed_without_saving_a_miss(tmp_path):
    config = tmp_path / "config.json"
    state = tmp_path / "state"
    config.write_text(json.dumps({"inputs": {"A": {"source": "srt", "format": "1080i59.94"}}}), encoding="utf-8")

    def probe(argv):
        assert any(item.startswith("srt://0.0.0.0:9001") for item in argv)
        return "bind: Address already in use\n"

    found = detect_channel("A", config, state, probe=probe)
    assert found["ok"] is False and "9001" in found["detail"]
    assert json.loads(config.read_text(encoding="utf-8"))["inputs"]["A"]["format"] == "1080i59.94"
