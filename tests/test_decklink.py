"""DeckLink list and status parsing. No card required."""

from nexencoder.decklink import ffmpeg_has_decklink, parse_list_devices, parse_status


LISTING = """
[decklink @ 0x1] Auto-detected 2 DeckLink devices
[decklink @ 0x1] 'DeckLink Duo (1)'
[decklink @ 0x1] 'DeckLink Duo (2)'
Immediate exit requested
"""


def test_parse_list_devices_keeps_order_and_names():
    devices = parse_list_devices(LISTING)
    assert devices == [
        {"index": 0, "name": "DeckLink Duo (1)"},
        {"index": 1, "name": "DeckLink Duo (2)"},
    ]


def test_missing_decklink_build():
    text = "Unknown input format: 'decklink'\n"
    assert ffmpeg_has_decklink(text) is False
    assert parse_list_devices(text) == []


def test_status_json_reports_lock_without_hiding_a_busy_input():
    blob = """
    {"devices": [
      {"name": "DeckLink Duo (1)", "input_locked": true, "input_mode": "1080i59.94", "busy": true},
      {"name": "DeckLink Duo (2)", "input_locked": false, "input_mode": "unknown", "busy": false}
    ]}
    """
    locked = parse_status(blob, "DeckLink Duo (1)")
    assert locked["signal"] == "present" and locked["format"] == "1080i59.94"
    assert "busy" in locked["detail"]
    quiet = parse_status(blob, "DeckLink Duo (2)")
    assert quiet["signal"] == "no_signal" and quiet["format"] == ""
    assert parse_status("{", "DeckLink Duo (1)")["signal"] == "unknown"
