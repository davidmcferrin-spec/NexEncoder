"""SDI VANC words match the playback patch: parity, checksum, and v210 packing."""

from nexencoder.cdp import cdp_from_a53, parse_cdp
from nexencoder.sdi_vanc import (
    CC_DID,
    CC_LINE,
    CC_SDID,
    SCTE_DID,
    SCTE_LINE,
    SCTE_SDID,
    anc_word,
    checksum_word,
    pack_v210,
    unpack_v210,
    vanc_words,
)


def test_anc_parity_and_checksum_cover_did_through_user_data():
    assert anc_word(0x61) & 0xFF == 0x61
    word = anc_word(0x61)
    ones = bin(0x61).count("1") & 1
    assert ((word >> 8) & 1) == ones
    assert ((word >> 9) & 1) == (ones ^ 1)
    payload = bytes([0x96, 0x69, 0x0B])
    words = vanc_words(CC_DID, CC_SDID, payload)
    assert words[:3] == [0x000, 0x3FF, 0x3FF]
    assert words[3:6] == [anc_word(CC_DID), anc_word(CC_SDID), anc_word(len(payload))]
    assert words[-1] == checksum_word(words[3:-1])
    assert CC_LINE == 11 and SCTE_LINE == 13
    assert (SCTE_DID, SCTE_SDID) == (0x41, 0x07)


def test_v210_round_trip_pads_to_three_words():
    words = vanc_words(SCTE_DID, SCTE_SDID, b"\xff\xff\x00")
    packed = pack_v210(words)
    assert len(packed) % 4 == 0
    assert unpack_v210(packed, len(words)) == words


def test_a53_triplets_become_a_cdp():
    cdp = cdp_from_a53(b"\xfc\x80\x80\xfd\x80\x80", 4, 7)
    parsed = parse_cdp(cdp)
    assert parsed.frame_rate_code == 4
    assert parsed.sequence == 7
    assert [triplet.cc_type for triplet in parsed.triplets] == [0, 1]
    assert cdp[:2] == b"\x96\x69"
