"""Combined caption decoding: one CDP in, 608 and 708 caption events out."""

from __future__ import annotations

from typing import Union

from .cdp import CC_608_FIELD1, CC_608_FIELD2, parse_cdp
from .cea608 import Caption608Event, Cea608Decoder
from .cea708 import Caption708Event, Cea708Decoder

CaptionEvent = Union[Caption608Event, Caption708Event]


class CaptionDecoder:
    """Decodes CEA-608 and CEA-708 carried in a stream of CDPs."""

    def __init__(self) -> None:
        self.cea608 = Cea608Decoder()
        self.cea708 = Cea708Decoder()

    def feed_cdp(self, data: bytes) -> list[CaptionEvent]:
        """Decode one CDP. Raises ParseError if the CDP itself is malformed."""
        cdp = parse_cdp(data)
        events: list[CaptionEvent] = []
        for t in cdp.triplets:
            if t.valid and t.cc_type in (CC_608_FIELD1, CC_608_FIELD2):
                events += self.cea608.feed(t.cc_type + 1, t.data1, t.data2)
        events += self.cea708.feed(cdp.triplets)
        return events
