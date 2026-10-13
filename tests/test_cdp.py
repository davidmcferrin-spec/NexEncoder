import pytest

from nexencoder.cdp import (CC_608_FIELD1, CC_DTVCC_DATA, CC_DTVCC_START, CcTriplet,
                            build_cdp, parse_cdp)
from nexencoder.errors import ParseError


def _triplets():
    return [
        CcTriplet(True, CC_608_FIELD1, 0x94, 0x2C),
        CcTriplet(True, CC_DTVCC_START, 0x42, 0xFE),
        CcTriplet(False, CC_DTVCC_DATA, 0x00, 0x00),
    ]


def test_round_trip():
    raw = build_cdp(4, 1234, _triplets())
    cdp = parse_cdp(raw)
    assert cdp.sequence == 1234
    assert cdp.frame_rate == 29.97
    assert list(cdp.triplets) == _triplets()
    assert sum(raw) & 0xFF == 0


def test_trailing_bytes_after_cdp_ignored():
    raw = build_cdp(4, 1, _triplets())
    assert parse_cdp(raw + b"\x00\x00").sequence == 1


def test_checksum_failure():
    raw = bytearray(build_cdp(4, 1, _triplets()))
    raw[9] ^= 0x10
    with pytest.raises(ParseError, match="checksum"):
        parse_cdp(bytes(raw))
    assert parse_cdp(bytes(raw), verify_checksum=False).sequence == 1


def test_bad_identifier_and_short():
    with pytest.raises(ParseError):
        parse_cdp(b"\x00" * 20)
    with pytest.raises(ParseError):
        parse_cdp(b"\x96\x69\x05")


def test_length_exceeds_data():
    raw = build_cdp(4, 1, _triplets())
    with pytest.raises(ParseError):
        parse_cdp(raw[:-2])


def test_too_many_triplets():
    with pytest.raises(ValueError):
        build_cdp(4, 1, [CcTriplet(True, 0, 0, 0)] * 32)
