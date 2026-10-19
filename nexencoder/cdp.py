"""Caption Distribution Packet (SMPTE 334-2 / CEA-708 CDP) parser and builder.

A CDP carries cc_data triplets: CEA-608 field 1/2 byte pairs (cc_type 0/1) and DTVCC
packet bytes (cc_type 2 = continuation, 3 = packet start).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .errors import ParseError

log = logging.getLogger(__name__)

CDP_ID = b"\x96\x69"
SEC_TIMECODE = 0x71
SEC_CCDATA = 0x72
SEC_SVCINFO = 0x73
SEC_FOOTER = 0x74

CC_608_FIELD1, CC_608_FIELD2, CC_DTVCC_DATA, CC_DTVCC_START = 0, 1, 2, 3

FRAME_RATES = {1: 23.976, 2: 24.0, 3: 25.0, 4: 29.97, 5: 30.0, 6: 50.0, 7: 59.94, 8: 60.0}


@dataclass(frozen=True)
class CcTriplet:
    valid: bool
    cc_type: int
    data1: int
    data2: int


@dataclass(frozen=True)
class Cdp:
    frame_rate_code: int
    sequence: int
    triplets: tuple[CcTriplet, ...] = field(default_factory=tuple)
    timecode: Optional[str] = None
    caption_service_active: bool = True

    @property
    def frame_rate(self) -> Optional[float]:
        return FRAME_RATES.get(self.frame_rate_code)


def _bcd_timecode(b: bytes) -> str:
    hours = ((b[0] >> 4) & 0x3) * 10 + (b[0] & 0x0F)
    minutes = ((b[1] >> 4) & 0x7) * 10 + (b[1] & 0x0F)
    seconds = ((b[2] >> 4) & 0x7) * 10 + (b[2] & 0x0F)
    frames = ((b[3] >> 4) & 0x3) * 10 + (b[3] & 0x0F)
    sep = ";" if b[3] & 0x80 else ":"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{sep}{frames:02d}"


def parse_cdp(data: bytes, verify_checksum: bool = True) -> Cdp:
    """Parse one CDP. Raises ParseError on any structural or checksum fault."""
    if len(data) < 11:
        raise ParseError("CDP too short")
    if data[0:2] != CDP_ID:
        raise ParseError("bad cdp_identifier")
    length = data[2]
    if length > len(data):
        raise ParseError(f"cdp_length {length} exceeds available {len(data)} bytes")
    cdp = data[:length]
    if verify_checksum and sum(cdp) & 0xFF != 0:
        raise ParseError("CDP checksum mismatch")

    frame_rate_code = cdp[3] >> 4
    flags = cdp[4]
    time_code_present = bool(flags & 0x80)
    ccdata_present = bool(flags & 0x40)
    svc_info_present = bool(flags & 0x20)
    active = bool(flags & 0x02)
    sequence = int.from_bytes(cdp[5:7], "big")
    pos = 7

    def need(n: int) -> None:
        if pos + n > len(cdp):
            raise ParseError("truncated CDP")

    timecode = None
    if time_code_present:
        need(5)
        if cdp[pos] != SEC_TIMECODE:
            raise ParseError("expected time_code_section")
        timecode = _bcd_timecode(cdp[pos + 1 : pos + 5])
        pos += 5

    triplets: list[CcTriplet] = []
    if ccdata_present:
        need(2)
        if cdp[pos] != SEC_CCDATA:
            raise ParseError("expected ccdata_section")
        count = cdp[pos + 1] & 0x1F
        pos += 2
        need(count * 3)
        for i in range(count):
            b0, d1, d2 = cdp[pos + 3 * i : pos + 3 * i + 3]
            triplets.append(CcTriplet(bool(b0 & 0x04), b0 & 0x03, d1, d2))
        pos += count * 3

    if svc_info_present:
        need(2)
        if cdp[pos] != SEC_SVCINFO:
            raise ParseError("expected ccsvcinfo_section")
        count = cdp[pos + 1] & 0x0F
        pos += 2
        need(count * 7)
        pos += count * 7

    need(4)
    if cdp[pos] != SEC_FOOTER:
        raise ParseError("expected cdp_footer")
    return Cdp(frame_rate_code, sequence, tuple(triplets), timecode, active)


def build_cdp(frame_rate_code: int, sequence: int, triplets: list[CcTriplet]) -> bytes:
    """Build a CDP (no timecode, no service info) with a valid checksum."""
    if len(triplets) > 31:
        raise ValueError("a CDP carries at most 31 cc_data triplets")
    body = bytes([(frame_rate_code << 4) | 0x0F, 0x43]) + (sequence & 0xFFFF).to_bytes(2, "big")
    body += bytes([SEC_CCDATA, 0xE0 | len(triplets)])
    for t in triplets:
        body += bytes([0xF8 | (0x04 if t.valid else 0) | (t.cc_type & 0x03), t.data1, t.data2])
    body += bytes([SEC_FOOTER]) + (sequence & 0xFFFF).to_bytes(2, "big")
    total = 3 + len(body) + 1
    partial = CDP_ID + bytes([total]) + body
    return partial + bytes([(-sum(partial)) & 0xFF])


def cdp_from_a53(data: bytes, frame_rate_code: int, sequence: int) -> bytes:
    """Wrap A/53 cc_data triplets in a CDP. Each triplet is marker, byte, byte."""
    if frame_rate_code not in FRAME_RATES:
        raise ValueError("CDP frame rate code must be 1 to 8")
    if len(data) % 3:
        raise ValueError("A/53 cc_data length must be a multiple of 3")
    if len(data) > 31 * 3:
        raise ValueError("a CDP carries at most 31 cc_data triplets")
    triplets = [
        CcTriplet(bool(data[i] & 0x04), data[i] & 0x03, data[i + 1], data[i + 2])
        for i in range(0, len(data), 3)
    ]
    return build_cdp(frame_rate_code, sequence, triplets)
