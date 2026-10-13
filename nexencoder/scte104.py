"""SCTE-104 multiple_operation_message (MOM) parser and builder.

Scope: the reassembled message bytes. Extracting and reassembling the message from
SMPTE 2010 ANC packets (DID 0x41 / SDID 0x07) belongs to the capture layer.
Single_operation_message framing is rejected with a clear error.

Supported operations: splice_request (0x0101), splice_null (0x0102),
time_signal_request (0x0104). Anything else is surfaced as UnknownOp, never dropped.

message_size is treated as the byte count of the whole message including the leading
0xFFFF; trailing bytes beyond it are ignored. Verify this convention against real
captures (see tests/test_scte104.py and captures/).
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from typing import Union

from .errors import ParseError

log = logging.getLogger(__name__)

OP_SPLICE_REQUEST = 0x0101
OP_SPLICE_NULL = 0x0102
OP_TIME_SIGNAL = 0x0104

# splice_insert_type values
SPLICE_START_NORMAL = 1
SPLICE_START_IMMEDIATE = 2
SPLICE_END_NORMAL = 3
SPLICE_END_IMMEDIATE = 4
SPLICE_CANCEL = 5

TIME_NONE, TIME_UTC, TIME_VITC, TIME_GPI = 0, 1, 2, 3
_TIME_LEN = {TIME_NONE: 0, TIME_UTC: 6, TIME_VITC: 4, TIME_GPI: 2}

_SPLICE_REQ = struct.Struct(">BIHHHBBB")  # 14 bytes


@dataclass(frozen=True)
class SpliceRequest:
    splice_insert_type: int
    splice_event_id: int
    unique_program_id: int
    pre_roll_ms: int
    break_duration_ds: int  # tenths of a second
    avail_num: int = 0
    avails_expected: int = 0
    auto_return: bool = False


@dataclass(frozen=True)
class SpliceNull104:
    pass


@dataclass(frozen=True)
class TimeSignalRequest:
    pre_roll_ms: int


@dataclass(frozen=True)
class UnknownOp:
    op_id: int
    data: bytes


Operation = Union[SpliceRequest, SpliceNull104, TimeSignalRequest, UnknownOp]


@dataclass(frozen=True)
class Mom:
    protocol_version: int
    as_index: int
    message_number: int
    dpi_pid_index: int
    scte35_protocol_version: int
    time_type: int
    time_data: bytes
    operations: tuple[Operation, ...] = field(default_factory=tuple)


def _parse_op(op_id: int, data: bytes) -> Operation:
    if op_id == OP_SPLICE_REQUEST:
        if len(data) != _SPLICE_REQ.size:
            raise ParseError(f"splice_request data is {len(data)} bytes, expected {_SPLICE_REQ.size}")
        t, eid, upid, pre, dur, an, ae, ar = _SPLICE_REQ.unpack(data)
        return SpliceRequest(t, eid, upid, pre, dur, an, ae, bool(ar))
    if op_id == OP_SPLICE_NULL:
        if data:
            raise ParseError("splice_null carries unexpected data")
        return SpliceNull104()
    if op_id == OP_TIME_SIGNAL:
        if len(data) != 2:
            raise ParseError(f"time_signal_request data is {len(data)} bytes, expected 2")
        return TimeSignalRequest(int.from_bytes(data, "big"))
    return UnknownOp(op_id, data)


def parse_mom(data: bytes) -> Mom:
    """Parse a reassembled SCTE-104 multiple_operation_message."""
    if len(data) < 4:
        raise ParseError("SCTE-104 message too short")
    if data[0:2] != b"\xff\xff":
        raise ParseError("not a multiple_operation_message (single_operation_message unsupported)")
    size = int.from_bytes(data[2:4], "big")
    if size > len(data):
        raise ParseError(f"message_size {size} exceeds available {len(data)} bytes")
    if size < 4 + 8:
        raise ParseError(f"message_size {size} too small")
    msg = data[:size]
    pos = 4

    def take(n: int) -> bytes:
        nonlocal pos
        if pos + n > len(msg):
            raise ParseError("truncated SCTE-104 message")
        chunk = msg[pos : pos + n]
        pos += n
        return chunk

    protocol_version = take(1)[0]
    as_index = take(1)[0]
    message_number = take(1)[0]
    dpi_pid_index = int.from_bytes(take(2), "big")
    scte35_pv = take(1)[0]
    time_type = take(1)[0]
    if time_type not in _TIME_LEN:
        raise ParseError(f"unknown time_type {time_type}")
    time_data = take(_TIME_LEN[time_type])
    num_ops = take(1)[0]
    ops: list[Operation] = []
    for _ in range(num_ops):
        op_id = int.from_bytes(take(2), "big")
        length = int.from_bytes(take(2), "big")
        ops.append(_parse_op(op_id, take(length)))
    if pos != len(msg):
        log.warning("SCTE-104: %d unparsed trailing bytes inside message_size", len(msg) - pos)
    return Mom(protocol_version, as_index, message_number, dpi_pid_index, scte35_pv,
               time_type, time_data, tuple(ops))


def _encode_op(op: Operation) -> bytes:
    if isinstance(op, SpliceRequest):
        body = _SPLICE_REQ.pack(op.splice_insert_type, op.splice_event_id, op.unique_program_id,
                                op.pre_roll_ms, op.break_duration_ds, op.avail_num,
                                op.avails_expected, int(op.auto_return))
        op_id = OP_SPLICE_REQUEST
    elif isinstance(op, SpliceNull104):
        body, op_id = b"", OP_SPLICE_NULL
    elif isinstance(op, TimeSignalRequest):
        body, op_id = op.pre_roll_ms.to_bytes(2, "big"), OP_TIME_SIGNAL
    elif isinstance(op, UnknownOp):
        body, op_id = op.data, op.op_id
    else:
        raise TypeError(f"unsupported operation: {type(op).__name__}")
    return op_id.to_bytes(2, "big") + len(body).to_bytes(2, "big") + body


def build_mom(mom: Mom) -> bytes:
    """Serialize a Mom. Used for tests, fixtures, and loopback injection."""
    if len(mom.time_data) != _TIME_LEN[mom.time_type]:
        raise ValueError("time_data length does not match time_type")
    body = bytes([mom.protocol_version, mom.as_index, mom.message_number])
    body += mom.dpi_pid_index.to_bytes(2, "big")
    body += bytes([mom.scte35_protocol_version, mom.time_type]) + mom.time_data
    body += bytes([len(mom.operations)])
    for op in mom.operations:
        body += _encode_op(op)
    size = 4 + len(body)
    if size > 0xFFFF:
        raise ValueError("SCTE-104 message too large")
    return b"\xff\xff" + size.to_bytes(2, "big") + body
