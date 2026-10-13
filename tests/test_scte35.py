import base64

import pytest

from nexencoder.crc import crc32_mpeg
from nexencoder.errors import ParseError
from nexencoder.scte35 import (SpliceInsert, SpliceNull, TimeSignal,
                               build_section, parse_section)


def test_crc_check_value():
    assert crc32_mpeg(b"123456789") == 0x0376E6E7


def test_external_vector_parses_with_valid_crc():
    # Widely published splice_insert example with an avail descriptor.
    raw = base64.b64decode("/DAvAAAAAAAA///wFAVIAACPf+/+c2nALv4AUsz1AAAAAAAKAAhDVUVJAAABNWLbowo=")
    sec = parse_section(raw)
    cmd = sec.command
    assert isinstance(cmd, SpliceInsert)
    assert cmd.event_id == 0x4800008F
    assert cmd.out_of_network is True
    assert cmd.auto_return is True
    assert cmd.break_duration == 0x52CCF5
    assert len(sec.descriptors) == 10


@pytest.mark.parametrize("cmd", [
    SpliceNull(),
    TimeSignal(pts_time=123456789),
    TimeSignal(pts_time=None),
    SpliceInsert(event_id=7, cancel=True),
    SpliceInsert(event_id=1, out_of_network=True, pts_time=900000,
                 break_duration=2700000, auto_return=True, unique_program_id=5,
                 avail_num=1, avails_expected=2),
    SpliceInsert(event_id=2, out_of_network=False, immediate=True, unique_program_id=9),
])
def test_round_trip(cmd):
    sec = parse_section(build_section(cmd, pts_adjustment=42, tier=0x123))
    assert sec.command == cmd
    assert sec.pts_adjustment == 42
    assert sec.tier == 0x123


def test_crc_corruption_detected():
    data = bytearray(build_section(SpliceNull()))
    data[8] ^= 0x01
    with pytest.raises(ParseError, match="CRC"):
        parse_section(bytes(data))


def test_bad_table_id():
    data = bytearray(build_section(SpliceNull()))
    data[0] = 0x00
    with pytest.raises(ParseError, match="table_id"):
        parse_section(bytes(data))


def test_truncated():
    data = build_section(SpliceInsert(event_id=1, out_of_network=True, pts_time=1))
    with pytest.raises(ParseError):
        parse_section(data[:-3])
    with pytest.raises(ParseError):
        parse_section(b"\xfc\x30")


def test_pts_wraps_to_33_bits():
    cmd = TimeSignal(pts_time=(1 << 33) + 5)
    assert parse_section(build_section(cmd)).command == TimeSignal(pts_time=5)
