"""VANC words for an SDI playback line.

CEA-708 captions travel as a CDP (DID 0x61 / SDID 0x01). SCTE-104 travels as
the multiple_operation_message itself (DID 0x41 / SDID 0x07). The DeckLink
playback patch in ``sdi_patch`` writes the same 10-bit words.
"""

from __future__ import annotations

CC_DID, CC_SDID = 0x61, 0x01
SCTE_DID, SCTE_SDID = 0x41, 0x07
CC_LINE, SCTE_LINE = 11, 13


def anc_word(value: int) -> int:
    """8-bit value with even parity in bit 8 and its inverse in bit 9."""
    byte = value & 0xFF
    ones = bin(byte).count("1") & 1
    return byte | (ones << 8) | ((ones ^ 1) << 9)


def checksum_word(words: list[int]) -> int:
    """SMPTE ST 291 checksum over the 9-bit words from DID through the last user word."""
    total = 0
    for word in words:
        total = (total + (word & 0x1FF)) & 0x1FF
    bit9 = 0 if total & 0x100 else 1
    return total | (bit9 << 9)


def vanc_words(did: int, sdid: int, payload: bytes) -> list[int]:
    """ADF, header, user data, and checksum for one ancillary packet."""
    if len(payload) > 255:
        raise ValueError("VANC payload is longer than 255 bytes")
    body = [anc_word(did), anc_word(sdid), anc_word(len(payload))]
    body.extend(anc_word(byte) for byte in payload)
    return [0x000, 0x3FF, 0x3FF, *body, checksum_word(body)]


def pack_v210(words: list[int]) -> bytes:
    """Three 10-bit words per little-endian v210 group. The tail is padded with zeros."""
    padded = list(words)
    while len(padded) % 3:
        padded.append(0)
    out = bytearray()
    for index in range(0, len(padded), 3):
        w0, w1, w2 = padded[index:index + 3]
        group = (w0 & 0x3FF) | ((w1 & 0x3FF) << 10) | ((w2 & 0x3FF) << 20)
        out += group.to_bytes(4, "little")
    return bytes(out)


def unpack_v210(data: bytes, count: int) -> list[int]:
    """Read ``count`` 10-bit words back out of a v210 line. Used by tests."""
    words: list[int] = []
    for index in range(0, len(data), 4):
        group = int.from_bytes(data[index:index + 4], "little")
        words.extend((group & 0x3FF, (group >> 10) & 0x3FF, (group >> 20) & 0x3FF))
        if len(words) >= count:
            return words[:count]
    raise ValueError("v210 line is shorter than the word count")
