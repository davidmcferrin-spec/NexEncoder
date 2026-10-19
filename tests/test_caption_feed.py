"""Vendor caption datagrams become the NXCC file the encode filter reads."""

from nexencoder.caption_feed import (
    MODE_EVERY,
    MODE_IDLE,
    MODE_ONCE,
    CaptionIngest,
    cc_from_text,
    parse_caption_file,
    read_caption_source,
    write_caption_source,
)
from nexencoder.cdp import CcTriplet, build_cdp


def test_text_is_608_once_and_a_repeat_does_not_roll(tmp_path):
    write_caption_source(tmp_path, "A", "vendor")
    ingest = CaptionIngest("A", tmp_path)
    assert ingest.handle(b"\x01Hello", 10.0) == "applied"
    mode, seq, data = parse_caption_file(ingest.path.read_bytes())
    expected, rolling = cc_from_text("Hello", rolling=False)
    assert mode == MODE_ONCE and seq == 1 and data == expected and rolling
    assert ingest.handle(b"\x01Hello", 11.0) == "applied"
    assert parse_caption_file(ingest.path.read_bytes())[1] == 1
    assert ingest.handle(b"\x01Next line", 12.0) == "applied"
    mode, seq, data = parse_caption_file(ingest.path.read_bytes())
    rolled, _rolling = cc_from_text("Next line", rolling=True)
    assert mode == MODE_ONCE and seq == 2 and data == rolled


def test_cdp_and_raw_triplets_attach_every_frame(tmp_path):
    write_caption_source(tmp_path, "A", "vendor")
    ingest = CaptionIngest("A", tmp_path)
    cdp = build_cdp(4, 7, [CcTriplet(True, 0, 0x80, 0x80)])
    assert ingest.handle(b"\x02" + cdp, 1.0) == "applied"
    mode, _seq, data = parse_caption_file(ingest.path.read_bytes())
    assert mode == MODE_EVERY and data == bytes([0xFC, 0x80, 0x80])
    assert ingest.handle(b"\x03\xfd\x80\x80", 2.0) == "applied"
    mode, seq, data = parse_caption_file(ingest.path.read_bytes())
    assert mode == MODE_EVERY and seq == 2 and data == bytes([0xFD, 0x80, 0x80])


def test_vanc_ignores_vendor_packets_and_clear_returns_to_idle(tmp_path):
    ingest = CaptionIngest("B", tmp_path)
    assert read_caption_source(tmp_path, "B") == "vanc"
    assert ingest.handle(b"\x01Hello", 1.0) == "ignored"
    assert not ingest.path.exists()
    write_caption_source(tmp_path, "B", "vendor")
    assert ingest.handle(b"\x01Hello", 2.0) == "applied"
    assert ingest.handle(b"\x00", 3.0) == "cleared"
    mode, _seq, data = parse_caption_file(ingest.path.read_bytes())
    assert mode == MODE_IDLE and data == b""
    assert write_caption_source(tmp_path, "B", "vanc") == "vanc"
    mode, _seq, data = parse_caption_file(ingest.path.read_bytes())
    assert mode == MODE_IDLE and data == b""


def test_hold_expiry_returns_the_file_to_idle(tmp_path):
    write_caption_source(tmp_path, "A", "vendor")
    ingest = CaptionIngest("A", tmp_path, hold_seconds=8.0)
    assert ingest.handle(b"\x01Hi", 10.0) == "applied"
    assert ingest.expire(17.9) is False
    assert ingest.expire(18.0) is True
    mode, _seq, data = parse_caption_file(ingest.path.read_bytes())
    assert mode == MODE_IDLE and data == b""


def test_bad_datagrams_are_rejected(tmp_path):
    write_caption_source(tmp_path, "A", "vendor")
    ingest = CaptionIngest("A", tmp_path)
    assert ingest.handle(b"", 1.0) == "rejected"
    assert ingest.handle(b"\x09nope", 1.0) == "rejected"
    assert ingest.handle(b"\x03\x00\x01", 1.0) == "rejected"
    assert ingest.handle(b"\x01", 1.0) == "rejected"
    assert not ingest.path.exists()
