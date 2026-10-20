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


def packet_pid(pkt: bytes) -> int:
    return ((pkt[1] & 0x1F) << 8) | pkt[2]


def _payload(pkt: bytes) -> bytes:
    afc = (pkt[3] >> 4) & 0x3
    if afc == 1:
        return pkt[4:]
    if afc == 3 and len(pkt) > 4:
        start = 5 + pkt[4]
        if start <= TS_PACKET:
            return pkt[start:]
    return b""


def _section(pkt: bytes) -> bytes | None:
    """First section in a payload-unit-start packet, after the pointer field."""
    if len(pkt) != TS_PACKET or pkt[0] != SYNC or not (pkt[1] & 0x40):
        return None
    payload = _payload(pkt)
    if not payload:
        return None
    start = 1 + payload[0]
    if start >= len(payload):
        return None
    return payload[start:]


def pat_program_pid(pkt: bytes) -> int | None:
    """PMT PID of the first real program in a PAT packet."""
    sec = _section(pkt)
    if sec is None or len(sec) < 12 or sec[0] != 0x00:
        return None
    limit = min(len(sec), 3 + (((sec[1] & 0x0F) << 8) | sec[2])) - 4
    index = 8
    while index + 4 <= limit:
        program = (sec[index] << 8) | sec[index + 1]
        pid = ((sec[index + 2] & 0x1F) << 8) | sec[index + 3]
        if program != 0:
            return pid
        index += 4
    return None


def pmt_video_pid(pkt: bytes) -> int | None:
    """Elementary PID of the H.264 stream in a PMT packet."""
    sec = _section(pkt)
    if sec is None or len(sec) < 16 or sec[0] != 0x02:
        return None
    limit = min(len(sec), 3 + (((sec[1] & 0x0F) << 8) | sec[2])) - 4
    info = ((sec[10] & 0x0F) << 8) | sec[11]
    index = 12 + info
    while index + 5 <= limit:
        stream_type = sec[index]
        pid = ((sec[index + 1] & 0x1F) << 8) | sec[index + 2]
        extra = ((sec[index + 3] & 0x0F) << 8) | sec[index + 4]
        if stream_type == 0x1B:
            return pid
        index += 5 + extra
    return None


def is_random_access(pkt: bytes) -> bool:
    """True when this packet begins a PES the muxer marked as a random access point."""
    if len(pkt) < 6 or pkt[0] != SYNC or not (pkt[1] & 0x40):
        return False
    afc = (pkt[3] >> 4) & 0x3
    if afc not in (2, 3) or pkt[4] < 1:
        return False
    return bool(pkt[5] & 0x40)


def _has_sps(pkt: bytes) -> bool:
    """True when the packet carries an H.264 sequence parameter set."""
    start = 0
    while True:
        found = pkt.find(b"\x00\x00\x01", start)
        if found < 0 or found + 3 >= len(pkt):
            return False
        if pkt[found + 3] & 0x1F == 7:
            return True
        start = found + 3


def _au_is_keyframe(packets: list[bytes]) -> bool:
    """True when this access unit is marked, or its sequence header has arrived.

    The start code may be split across the last bytes of one packet and the
    first bytes of the next, so the scan keeps a three-byte overlap.
    """
    if is_random_access(packets[0]):
        return True
    tail = b""
    for pkt in packets:
        if _has_sps(tail + pkt):
            return True
        tail = pkt[-3:]
    return False


class PreviewGate:
    """Hold a program feed until the first video keyframe, then pass it through.

    A decoder that starts on a predicted slice logs a missing parameter set
    for every slice until the next IDR, and that burst is what drops the
    RTSP session. The first bytes out are the cached PAT, the cached PMT,
    and the keyframe, so the picture starts on a random access point.
    The sequence header often rides in the packet after the PES start.
    Those packets stay held until the header arrives. A frame that ends
    without one is dropped when the next frame starts.
    """

    _HOLD_LIMIT = 8 * 1024 * 1024

    def __init__(self) -> None:
        self.opened = False
        self._buf = b""
        self._pat: bytes | None = None
        self._pmt_pid: int | None = None
        self._pmt: bytes | None = None
        self._video_pid: int | None = None
        self._hold: list[bytes] = []

    def feed(self, data: bytes) -> bytes:
        if not data:
            return b""
        self._buf += data
        out = bytearray()
        while len(self._buf) >= TS_PACKET:
            if self._buf[0] != SYNC:
                nxt = self._buf.find(bytes([SYNC]), 1)
                if nxt < 0:
                    self._buf = self._buf[-(TS_PACKET - 1):]
                    break
                self._buf = self._buf[nxt:]
                continue
            pkt = self._buf[:TS_PACKET]
            self._buf = self._buf[TS_PACKET:]
            out += self._consider(pkt)
        return bytes(out)

    def _consider(self, pkt: bytes) -> bytes:
        pid = packet_pid(pkt)
        if pid == 0:
            found = pat_program_pid(pkt)
            if found is not None:
                self._pat = pkt
                self._pmt_pid = found
        elif self._pmt_pid is not None and pid == self._pmt_pid:
            video = pmt_video_pid(pkt)
            if video is not None:
                if video != self._video_pid:
                    self._hold = []
                self._pmt = pkt
                self._video_pid = video
        if self.opened:
            return pkt
        # A keyframe seen before the tables is not reused. Its continuation
        # packets were dropped, so the next random access point is the start.
        if self._pat is None or self._pmt is None or self._video_pid is None or pid != self._video_pid:
            return b""
        return self._hold_video(pkt)

    def _hold_video(self, pkt: bytes) -> bytes:
        if pkt[1] & 0x40:
            self._hold = [pkt]
        elif self._hold:
            self._hold.append(pkt)
        else:
            return b""
        if sum(len(item) for item in self._hold) > self._HOLD_LIMIT:
            self._hold = []
            return b""
        if not _au_is_keyframe(self._hold):
            return b""
        self.opened = True
        out = self._pat + self._pmt + b"".join(self._hold)
        self._hold = []
        return out
