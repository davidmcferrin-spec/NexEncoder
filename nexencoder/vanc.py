"""Pull CEA-708 CDPs and SCTE-104 messages out of a VANC byte buffer.

The DeckLink demuxer forwards ANC user data here: a CDP starts with 0x96 0x69
and a SCTE-104 multiple_operation_message starts with 0xFFFF. Each hit is parsed
with the production parsers; a bad packet is skipped so one corrupt line does
not drop the rest of the frame.
"""

from __future__ import annotations

from .captions import CaptionDecoder
from .cdp import CDP_ID, parse_cdp
from .errors import ParseError
from .scte104 import parse_mom

_MOM = b"\xff\xff"


def scan_vanc(payload: bytes) -> dict[str, list]:
    captions = []
    splices = []
    start = 0
    while True:
        cdp_at = payload.find(CDP_ID, start)
        mom_at = payload.find(_MOM, start)
        hits = [at for at in (cdp_at, mom_at) if at >= 0]
        if not hits:
            break
        at = min(hits)
        if at == cdp_at:
            try:
                captions.append(parse_cdp(payload[at:]))
            except ParseError:
                pass
        else:
            try:
                splices.append(parse_mom(payload[at:]))
            except ParseError:
                pass
        start = at + 2
    return {"captions": captions, "splices": splices}


def events_from_vanc(channel_id: str, payload: bytes) -> list[dict[str, str]]:
    """Caption text and splice notes a monitor page can show."""
    found = scan_vanc(payload)
    events: list[dict[str, str]] = []
    decoder = CaptionDecoder()
    start = 0
    while True:
        at = payload.find(CDP_ID, start)
        if at < 0:
            break
        try:
            for caption in decoder.feed_cdp(payload[at:]):
                text = caption.text.strip()
                if not text:
                    continue
                if hasattr(caption, "channel"):
                    label = f"CC{caption.channel}"
                else:
                    label = f"SVC{caption.service}"
                events.append({"channel": channel_id, "kind": "caption", "label": label, "text": text})
        except ParseError:
            pass
        start = at + 2
    for mom in found["splices"]:
        events.append({
            "channel": channel_id,
            "kind": "scte104",
            "label": "SCTE-104",
            "text": f"{len(mom.operations)} operation(s)",
        })
    return events
