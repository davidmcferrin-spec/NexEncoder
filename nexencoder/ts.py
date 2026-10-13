"""MPEG-TS packetization of private sections (SCTE-35) and section extraction.

Packetizer: wraps a section in 188-byte packets (pointer_field 0, 0xFF stuffing,
per-PID continuity counter). Extractor: reassembles sections from one PID, honouring
pointer_field, adaptation fields, back-to-back sections, duplicate packets, and
continuity errors (a gap discards any partial section and is counted).
"""

from __future__ import annotations

import logging
from typing import Iterator

from .errors import ParseError

log = logging.getLogger(__name__)

TS_PACKET = 188
SYNC = 0x47
_PAYLOAD = TS_PACKET - 4


def iter_packets(data: bytes) -> Iterator[bytes]:
    """Yield 188-byte packets from ``data``. Raises ParseError on bad sync or length."""
    if len(data) % TS_PACKET:
        raise ParseError(f"TS data length {len(data)} is not a multiple of {TS_PACKET}")
    for i in range(0, len(data), TS_PACKET):
        pkt = data[i : i + TS_PACKET]
        if pkt[0] != SYNC:
            raise ParseError(f"lost TS sync at offset {i}")
        yield pkt


class SectionPacketizer:
    """Packetizes sections onto one PID, keeping the continuity counter across calls."""

    def __init__(self, pid: int) -> None:
        if not 0x0010 <= pid <= 0x1FFE:
            raise ValueError(f"PID 0x{pid:04X} is outside the usable range")
        self.pid = pid
        self._cc = 0

    def packetize(self, section: bytes) -> list[bytes]:
        if not section:
            raise ValueError("empty section")
        payload = b"\x00" + section  # pointer_field = 0
        packets = []
        for i in range(0, len(payload), _PAYLOAD):
            chunk = payload[i : i + _PAYLOAD]
            chunk += b"\xff" * (_PAYLOAD - len(chunk))
            pusi = 0x40 if i == 0 else 0x00
            header = bytes([SYNC, pusi | (self.pid >> 8), self.pid & 0xFF, 0x10 | self._cc])
            self._cc = (self._cc + 1) & 0x0F
            packets.append(header + chunk)
        return packets


class SectionExtractor:
    """Reassembles complete sections from the packets of a single PID."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._buf = bytearray()
        self._last_cc: int | None = None
        self.cc_errors = 0
        self.sync_errors = 0
        self.duplicates = 0

    def feed(self, pkt: bytes) -> list[bytes]:
        if len(pkt) != TS_PACKET or pkt[0] != SYNC:
            self.sync_errors += 1
            return []
        if ((pkt[1] & 0x1F) << 8 | pkt[2]) != self.pid:
            return []
        if pkt[1] & 0x80:  # transport_error_indicator
            self._buf.clear()
            return []
        afc = (pkt[3] >> 4) & 0x3
        if afc in (0, 2):  # reserved or adaptation-only: no payload, CC does not advance
            return []
        cc = pkt[3] & 0x0F
        if self._last_cc is not None:
            if cc == self._last_cc:
                self.duplicates += 1
                return []
            if cc != (self._last_cc + 1) & 0x0F:
                self.cc_errors += 1
                log.warning("PID 0x%04X continuity error: %d -> %d", self.pid, self._last_cc, cc)
                self._buf.clear()
        self._last_cc = cc

        pos = 4
        if afc == 3:
            pos += 1 + pkt[4]
        if pos > TS_PACKET:
            self.sync_errors += 1
            return []
        payload = pkt[pos:]

        out: list[bytes] = []
        if pkt[1] & 0x40:  # payload_unit_start_indicator
            if not payload:
                return []
            pointer = payload[0]
            if 1 + pointer > len(payload):
                self.sync_errors += 1
                self._buf.clear()
                return []
            if self._buf:
                self._buf += payload[1 : 1 + pointer]
                out += self._drain()
            self._buf = bytearray(payload[1 + pointer :])
            out += self._drain()
        elif self._buf:
            self._buf += payload
            out += self._drain()
        return out

    def _drain(self) -> list[bytes]:
        out = []
        while self._buf:
            if self._buf[0] == 0xFF:  # stuffing: no more sections in this packet
                self._buf.clear()
                break
            if len(self._buf) < 3:
                break
            total = 3 + (((self._buf[1] & 0x0F) << 8) | self._buf[2])
            if len(self._buf) < total:
                break
            out.append(bytes(self._buf[:total]))
            del self._buf[:total]
        return out


def extract_sections(data: bytes, pid: int) -> list[bytes]:
    """Convenience: all complete sections on ``pid`` in a TS byte string."""
    ex = SectionExtractor(pid)
    sections: list[bytes] = []
    for pkt in iter_packets(data):
        sections += ex.feed(pkt)
    return sections
