import pytest

from nexencoder import scte104 as s104
from nexencoder.errors import ParseError, TranslateError
from nexencoder.scte35 import (PTS_MOD, SpliceInsert, SpliceNull, TimeSignal,
                               parse_section)
from nexencoder.translate import translate_mom, translate_operation


def _mom(*ops, time_type=s104.TIME_NONE, time_data=b""):
    return s104.Mom(0, 0, 1, 0, 0, time_type, time_data, tuple(ops))


def _req(t, **kw):
    base = dict(splice_insert_type=t, splice_event_id=100, unique_program_id=3,
                pre_roll_ms=4000, break_duration_ds=300, avail_num=1, avails_expected=2,
                auto_return=True)
    base.update(kw)
    return s104.SpliceRequest(**base)


def test_mom_round_trip():
    mom = _mom(_req(s104.SPLICE_START_NORMAL), s104.SpliceNull104(),
               s104.TimeSignalRequest(2000), s104.UnknownOp(0x0109, b"\x01\x02"),
               time_type=s104.TIME_UTC, time_data=b"\x00\x00\x00\x01\x00\x02")
    assert s104.parse_mom(s104.build_mom(mom)) == mom


def test_trailing_padding_ignored():
    mom = _mom(s104.SpliceNull104())
    assert s104.parse_mom(s104.build_mom(mom) + b"\x00\x00\x00") == mom


@pytest.mark.parametrize("data", [b"", b"\xff\xff", b"\x01\x01\x00\x10" + bytes(12)])
def test_bad_header(data):
    with pytest.raises(ParseError):
        s104.parse_mom(data)


def test_size_exceeds_data():
    data = s104.build_mom(_mom(s104.SpliceNull104()))
    with pytest.raises(ParseError):
        s104.parse_mom(data[:-1])


def test_bad_splice_request_length():
    mom = _mom(s104.UnknownOp(s104.OP_SPLICE_REQUEST, b"\x00" * 5))
    with pytest.raises(ParseError, match="splice_request"):
        s104.parse_mom(s104.build_mom(mom))


def test_start_normal_translates_with_preroll_and_duration():
    cmd = translate_operation(_req(s104.SPLICE_START_NORMAL), frame_pts=90000)
    assert isinstance(cmd, SpliceInsert)
    assert cmd.out_of_network and not cmd.immediate and not cmd.cancel
    assert cmd.pts_time == 90000 + 4000 * 90
    assert cmd.break_duration == 300 * 9000
    assert cmd.auto_return is True
    assert (cmd.event_id, cmd.unique_program_id) == (100, 3)


def test_start_immediate_has_no_pts():
    cmd = translate_operation(_req(s104.SPLICE_START_IMMEDIATE), 1000)
    assert cmd.immediate and cmd.out_of_network and cmd.pts_time is None


def test_end_normal_and_immediate_have_no_duration():
    n = translate_operation(_req(s104.SPLICE_END_NORMAL), 0)
    i = translate_operation(_req(s104.SPLICE_END_IMMEDIATE), 0)
    assert not n.out_of_network and n.break_duration is None and n.pts_time == 360000
    assert not i.out_of_network and i.immediate and i.break_duration is None


def test_zero_duration_means_no_break_duration():
    cmd = translate_operation(_req(s104.SPLICE_START_NORMAL, break_duration_ds=0), 0)
    assert cmd.break_duration is None and cmd.auto_return is False


def test_cancel():
    cmd = translate_operation(_req(s104.SPLICE_CANCEL), 0)
    assert cmd == SpliceInsert(event_id=100, cancel=True)


def test_pts_wraps():
    cmd = translate_operation(_req(s104.SPLICE_START_NORMAL), PTS_MOD - 90)
    assert cmd.pts_time == (PTS_MOD - 90 + 360000) % PTS_MOD


def test_time_signal_and_null():
    assert translate_operation(s104.TimeSignalRequest(1000), 5) == TimeSignal(pts_time=5 + 90000)
    assert translate_operation(s104.SpliceNull104(), 0) == SpliceNull()


def test_invalid_insert_type():
    with pytest.raises(TranslateError):
        translate_operation(_req(99), 0)


def test_translate_mom_skips_unknown_and_emits_valid_sections():
    mom = _mom(_req(s104.SPLICE_START_NORMAL), s104.UnknownOp(0x0109, b""))
    sections = translate_mom(mom, frame_pts=0)
    assert len(sections) == 1
    parsed = parse_section(sections[0])
    assert isinstance(parsed.command, SpliceInsert)
    assert parsed.command.pts_time == 360000
