"""SCTE-35 splice_info_section builder and parser.

Supported commands: splice_null (0x00), splice_insert (0x05, program splice),
time_signal (0x06). Descriptors are carried as opaque bytes. All times are in
90 kHz ticks. Encrypted sections are rejected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Union

from .crc import crc32_mpeg
from .errors import ParseError

log = logging.getLogger(__name__)

PTS_MOD = 1 << 33
TABLE_ID = 0xFC
CMD_NULL = 0x00
CMD_INSERT = 0x05
CMD_TIME_SIGNAL = 0x06
_MIN_SECTION = 20  # header(3) + proto(1) + adj(5) + cw(1) + len(3) + type(1) + desc len(2) + crc(4)


@dataclass(frozen=True)
class SpliceNull:
    """splice_null: heartbeat, no payload."""


@dataclass(frozen=True)
class TimeSignal:
    """time_signal: ``pts_time`` is None when time_specified_flag is 0."""

    pts_time: Optional[int]


@dataclass(frozen=True)
class SpliceInsert:
    """splice_insert, program splice mode only."""

    event_id: int
    cancel: bool = False
    out_of_network: bool = False
    immediate: bool = False
    pts_time: Optional[int] = None
    break_duration: Optional[int] = None  # 90 kHz ticks
    auto_return: bool = False
    unique_program_id: int = 0
    avail_num: int = 0
    avails_expected: int = 0


Command = Union[SpliceNull, TimeSignal, SpliceInsert]


@dataclass(frozen=True)
class SpliceInfoSection:
    command: Command
    pts_adjustment: int = 0
    tier: int = 0xFFF
    protocol_version: int = 0
    descriptors: bytes = field(default=b"")


def _enc_splice_time(pts: Optional[int]) -> bytes:
    if pts is None:
        return b"\x7f"
    return ((1 << 39) | (0x3F << 33) | (pts % PTS_MOD)).to_bytes(5, "big")


def _enc_break_duration(duration: int, auto_return: bool) -> bytes:
    if not 0 <= duration < PTS_MOD:
        raise ValueError(f"break_duration out of range: {duration}")
    return ((int(auto_return) << 39) | (0x3F << 33) | duration).to_bytes(5, "big")


def _encode_command(cmd: Command) -> tuple[int, bytes]:
    if isinstance(cmd, SpliceNull):
        return CMD_NULL, b""
    if isinstance(cmd, TimeSignal):
        return CMD_TIME_SIGNAL, _enc_splice_time(cmd.pts_time)
    if isinstance(cmd, SpliceInsert):
        out = cmd.event_id.to_bytes(4, "big")
        if cmd.cancel:
            return CMD_INSERT, out + b"\xff"
        has_dur = cmd.break_duration is not None
        flags = (
            (int(cmd.out_of_network) << 7)
            | (1 << 6)  # program_splice_flag
            | (int(has_dur) << 5)
            | (int(cmd.immediate) << 4)
            | 0x0F
        )
        out += b"\x7f" + bytes([flags])
        if not cmd.immediate:
            out += _enc_splice_time(cmd.pts_time)
        if has_dur:
            out += _enc_break_duration(cmd.break_duration, cmd.auto_return)
        out += cmd.unique_program_id.to_bytes(2, "big")
        out += bytes([cmd.avail_num & 0xFF, cmd.avails_expected & 0xFF])
        return CMD_INSERT, out
    raise TypeError(f"unsupported command type: {type(cmd).__name__}")


def build_section(
    command: Command, pts_adjustment: int = 0, tier: int = 0xFFF
) -> bytes:
    """Serialize ``command`` into a complete splice_info_section with CRC."""
    cmd_type, cmd = _encode_command(command)
    if len(cmd) >= 0xFFF:
        raise ValueError("splice command too long")
    body = bytes([0])  # protocol_version
    body += (pts_adjustment % PTS_MOD).to_bytes(5, "big")  # encrypted=0, alg=0
    body += bytes([0])  # cw_index
    body += (((tier & 0xFFF) << 12) | len(cmd)).to_bytes(3, "big")
    body += bytes([cmd_type]) + cmd
    body += (0).to_bytes(2, "big")  # descriptor_loop_length
    section_length = len(body) + 4
    header = bytes([TABLE_ID, 0x30 | (section_length >> 8), section_length & 0xFF])
    payload = header + body
    return payload + crc32_mpeg(payload).to_bytes(4, "big")


class _Reader:
    def __init__(self, data: bytes) -> None:
        self._d = data
        self._p = 0

    def take(self, n: int) -> bytes:
        if self._p + n > len(self._d):
            raise ParseError("truncated SCTE-35 section")
        chunk = self._d[self._p : self._p + n]
        self._p += n
        return chunk

    def uint(self, n: int) -> int:
        return int.from_bytes(self.take(n), "big")

    def peek(self) -> int:
        if self._p >= len(self._d):
            raise ParseError("truncated SCTE-35 section")
        return self._d[self._p]


def _dec_splice_time(r: _Reader) -> Optional[int]:
    if r.peek() & 0x80:
        return r.uint(5) & (PTS_MOD - 1)
    r.take(1)
    return None


def _decode_insert(r: _Reader) -> SpliceInsert:
    event_id = r.uint(4)
    if r.uint(1) & 0x80:
        return SpliceInsert(event_id=event_id, cancel=True)
    flags = r.uint(1)
    out_of_network = bool(flags & 0x80)
    program = bool(flags & 0x40)
    has_dur = bool(flags & 0x20)
    immediate = bool(flags & 0x10)
    if not program:
        raise ParseError("component-mode splice_insert is not supported")
    pts = None if immediate else _dec_splice_time(r)
    duration = None
    auto_return = False
    if has_dur:
        raw = r.uint(5)
        auto_return = bool(raw >> 39 & 1)
        duration = raw & (PTS_MOD - 1)
    upid = r.uint(2)
    avail_num = r.uint(1)
    avails_expected = r.uint(1)
    return SpliceInsert(
        event_id=event_id,
        out_of_network=out_of_network,
        immediate=immediate,
        pts_time=pts,
        break_duration=duration,
        auto_return=auto_return,
        unique_program_id=upid,
        avail_num=avail_num,
        avails_expected=avails_expected,
    )


def parse_section(data: bytes) -> SpliceInfoSection:
    """Parse and CRC-verify a splice_info_section. Raises ParseError on any fault."""
    if len(data) < _MIN_SECTION:
        raise ParseError("SCTE-35 section too short")
    if data[0] != TABLE_ID:
        raise ParseError(f"bad table_id 0x{data[0]:02X}")
    section_length = ((data[1] & 0x0F) << 8) | data[2]
    total = 3 + section_length
    if len(data) < total:
        raise ParseError("SCTE-35 section shorter than section_length")
    section = data[:total]
    expected = int.from_bytes(section[-4:], "big")
    actual = crc32_mpeg(section[:-4])
    if expected != actual:
        raise ParseError(f"SCTE-35 CRC mismatch: got {expected:08X}, computed {actual:08X}")

    r = _Reader(section[3:-4])
    protocol_version = r.uint(1)
    adj_raw = r.uint(5)
    if adj_raw >> 39 & 1:
        raise ParseError("encrypted SCTE-35 sections are not supported")
    pts_adjustment = adj_raw & (PTS_MOD - 1)
    r.take(1)  # cw_index
    tail = r.uint(3)
    tier = tail >> 12
    cmd_len = tail & 0xFFF
    cmd_type = r.uint(1)
    cmd_bytes = r.take(cmd_len)
    cr = _Reader(cmd_bytes)
    if cmd_type == CMD_NULL:
        command: Command = SpliceNull()
    elif cmd_type == CMD_TIME_SIGNAL:
        command = TimeSignal(pts_time=_dec_splice_time(cr))
    elif cmd_type == CMD_INSERT:
        command = _decode_insert(cr)
    else:
        raise ParseError(f"unsupported splice_command_type 0x{cmd_type:02X}")
    desc_len = r.uint(2)
    descriptors = r.take(desc_len)
    return SpliceInfoSection(
        command=command,
        pts_adjustment=pts_adjustment,
        tier=tier,
        protocol_version=protocol_version,
        descriptors=descriptors,
    )
