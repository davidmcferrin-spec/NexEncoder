"""CRC-32/MPEG-2 as used by MPEG-TS private sections and SCTE-35.

Polynomial 0x04C11DB7, initial value 0xFFFFFFFF, no input/output reflection,
no final XOR. Check value for b"123456789" is 0x0376E6E7.
"""

from __future__ import annotations

_POLY = 0x04C11DB7


def _make_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ _POLY) if crc & 0x80000000 else (crc << 1)
            crc &= 0xFFFFFFFF
        table.append(crc)
    return tuple(table)


_TABLE = _make_table()


def crc32_mpeg(data: bytes) -> int:
    """Return the CRC-32/MPEG-2 of ``data`` as an unsigned 32-bit integer."""
    crc = 0xFFFFFFFF
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _TABLE[((crc >> 24) ^ b) & 0xFF]
    return crc
