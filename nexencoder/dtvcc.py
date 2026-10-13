"""DTVCC (CEA-708) caption channel packet assembly and service-block splitting.

cc_data triplets of type 3 start a DTVCC packet, type 2 continues it. A packet is
``packet_size_code * 2`` bytes (128 when the code is 0) including its header byte.
"""

from __future__ import annotations

import logging
from typing import Iterator

from .cdp import CC_DTVCC_DATA, CC_DTVCC_START, CcTriplet
from .errors import ParseError

log = logging.getLogger(__name__)


class DtvccAssembler:
    """Reassembles DTVCC packets from a stream of cc_data triplets."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._total = 0
        self._last_seq: int | None = None
        self.dropped_packets = 0
        self.orphan_bytes = 0
        self.sequence_errors = 0

    def feed(self, triplet: CcTriplet) -> list[bytes]:
        """Feed one triplet; returns any packets completed by it (zero or one)."""
        if not triplet.valid or triplet.cc_type not in (CC_DTVCC_DATA, CC_DTVCC_START):
            return []
        if triplet.cc_type == CC_DTVCC_START:
            if self._buf:
                self.dropped_packets += 1
                log.warning("DTVCC packet start while a packet was incomplete; dropping it")
            seq = triplet.data1 >> 6
            if self._last_seq is not None and seq != (self._last_seq + 1) % 4:
                self.sequence_errors += 1
                log.warning("DTVCC sequence gap: %s -> %s", self._last_seq, seq)
            self._last_seq = seq
            size_code = triplet.data1 & 0x3F
            self._total = 128 if size_code == 0 else size_code * 2
            self._buf = bytearray([triplet.data1, triplet.data2])
        else:
            if not self._buf:
                self.orphan_bytes += 2
                return []
            self._buf += bytes([triplet.data1, triplet.data2])
        if len(self._buf) >= self._total:
            packet = bytes(self._buf[: self._total])
            self._buf = bytearray()
            return [packet]
        return []


def split_service_blocks(packet: bytes) -> Iterator[tuple[int, bytes]]:
    """Yield ``(service_number, block_bytes)`` from one assembled DTVCC packet."""
    if not packet:
        raise ParseError("empty DTVCC packet")
    size_code = packet[0] & 0x3F
    total = 128 if size_code == 0 else size_code * 2
    data = packet[1:total]
    pos = 0
    while pos < len(data):
        header = data[pos]
        pos += 1
        if header == 0:  # null service header: padding to end of packet
            return
        service = header >> 5
        size = header & 0x1F
        if service == 7:
            if pos >= len(data):
                raise ParseError("truncated extended service header")
            service = data[pos] & 0x3F
            pos += 1
        if service == 0:
            return
        if pos + size > len(data):
            raise ParseError("service block exceeds packet")
        yield service, data[pos : pos + size]
        pos += size
