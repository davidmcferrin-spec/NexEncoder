"""Shared test builders for caption tests."""

from nexencoder.cdp import CC_DTVCC_DATA, CC_DTVCC_START, CcTriplet


def odd_parity(b: int) -> int:
    """Apply odd parity to a 7-bit value (CEA-608 transmission format)."""
    return b | (0x80 if bin(b).count("1") % 2 == 0 else 0)


def make_packet(seq: int, blocks: list[tuple[int, bytes]]) -> bytes:
    """Build a DTVCC packet (header + service blocks) with even total length."""
    body = b""
    for service, data in blocks:
        body += bytes([(service << 5) | len(data)]) + data
    if len(body) % 2 == 0:
        body += b"\x00"
    size_code = (len(body) + 1) // 2
    assert 0 < size_code < 64
    return bytes([(seq << 6) | size_code]) + body


def packet_to_triplets(packet: bytes) -> list[CcTriplet]:
    """Chunk an even-length DTVCC packet into a start triplet plus continuations."""
    out = []
    for i in range(0, len(packet), 2):
        kind = CC_DTVCC_START if i == 0 else CC_DTVCC_DATA
        out.append(CcTriplet(True, kind, packet[i], packet[i + 1]))
    return out
