import pytest
from helpers import make_packet, odd_parity, packet_to_triplets

from nexencoder.captions import CaptionDecoder
from nexencoder.cdp import CC_608_FIELD1, CcTriplet, build_cdp
from nexencoder.cea608 import Caption608Event
from nexencoder.cea708 import Caption708Event
from nexencoder.errors import ParseError

DF0 = bytes([0x98, 0x20, 0x70, 0x20, 0x02, 0x1F, 0x01])


def t608(b1, b2):
    return CcTriplet(True, CC_608_FIELD1, odd_parity(b1), odd_parity(b2))


def test_cdp_with_608_and_708_yields_both():
    triplets = [
        t608(0x14, 0x20), t608(0x14, 0x20),  # RCL x2
        t608(0x14, 0x70), t608(0x14, 0x70),  # PAC row 15 x2
        t608(ord("H"), ord("I")),
        t608(0x14, 0x2F), t608(0x14, 0x2F),  # EOC x2
    ] + packet_to_triplets(make_packet(0, [(1, DF0 + b"HI")]))
    raw = build_cdp(4, 1, triplets)
    events = CaptionDecoder().feed_cdp(raw)
    assert [e for e in events if isinstance(e, Caption608Event)][-1].lines == ("HI",)
    assert [e for e in events if isinstance(e, Caption708Event)][-1].lines == ("HI",)


def test_state_carries_across_cdps():
    dec = CaptionDecoder()
    first = build_cdp(4, 1, [t608(0x14, 0x29), t608(0x14, 0x29), t608(0x14, 0x70),
                             t608(0x14, 0x70)])
    assert dec.feed_cdp(first) == []
    second = build_cdp(4, 2, [t608(ord("O"), ord("K"))])
    events = dec.feed_cdp(second)
    assert events and events[-1].lines == ("OK",)


def test_malformed_cdp_raises():
    raw = bytearray(build_cdp(4, 1, [t608(0x14, 0x20)]))
    raw[-1] ^= 0xFF
    with pytest.raises(ParseError):
        CaptionDecoder().feed_cdp(bytes(raw))
