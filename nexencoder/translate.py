"""SCTE-104 to SCTE-35 translation.

``frame_pts`` is the 90 kHz PTS (in the output stream's timebase) of the video frame on
which the SCTE-104 message arrived. Pre-roll is added to it to place the splice point.
"""

from __future__ import annotations

import logging
from typing import Optional

from . import scte104 as s104
from .errors import TranslateError
from .scte35 import (PTS_MOD, Command, SpliceInsert, SpliceNull, TimeSignal,
                     build_section)

log = logging.getLogger(__name__)

TICKS_PER_MS = 90
TICKS_PER_DECISECOND = 9000


def _at(frame_pts: int, pre_roll_ms: int) -> int:
    return (frame_pts + pre_roll_ms * TICKS_PER_MS) % PTS_MOD


def translate_operation(op: s104.Operation, frame_pts: int) -> Optional[Command]:
    """Translate one SCTE-104 operation. Returns None for operations we do not map."""
    if isinstance(op, s104.SpliceNull104):
        return SpliceNull()
    if isinstance(op, s104.TimeSignalRequest):
        return TimeSignal(pts_time=_at(frame_pts, op.pre_roll_ms))
    if isinstance(op, s104.UnknownOp):
        log.warning("SCTE-104 op 0x%04X has no SCTE-35 mapping; skipped", op.op_id)
        return None
    if not isinstance(op, s104.SpliceRequest):
        raise TranslateError(f"unsupported operation type {type(op).__name__}")

    t = op.splice_insert_type
    if t == s104.SPLICE_CANCEL:
        return SpliceInsert(event_id=op.splice_event_id, cancel=True)
    if t not in (s104.SPLICE_START_NORMAL, s104.SPLICE_START_IMMEDIATE,
                 s104.SPLICE_END_NORMAL, s104.SPLICE_END_IMMEDIATE):
        raise TranslateError(f"unknown splice_insert_type {t}")

    starting = t in (s104.SPLICE_START_NORMAL, s104.SPLICE_START_IMMEDIATE)
    immediate = t in (s104.SPLICE_START_IMMEDIATE, s104.SPLICE_END_IMMEDIATE)
    duration = None
    if starting and op.break_duration_ds > 0:
        duration = op.break_duration_ds * TICKS_PER_DECISECOND
    return SpliceInsert(
        event_id=op.splice_event_id,
        out_of_network=starting,
        immediate=immediate,
        pts_time=None if immediate else _at(frame_pts, op.pre_roll_ms),
        break_duration=duration,
        auto_return=op.auto_return if duration is not None else False,
        unique_program_id=op.unique_program_id,
        avail_num=op.avail_num,
        avails_expected=op.avails_expected,
    )


def translate_mom(mom: s104.Mom, frame_pts: int) -> list[bytes]:
    """Translate every mappable operation in ``mom`` into a SCTE-35 section."""
    sections: list[bytes] = []
    for op in mom.operations:
        cmd = translate_operation(op, frame_pts)
        if cmd is not None:
            sections.append(build_section(cmd))
    return sections
