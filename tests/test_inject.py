import json
from fractions import Fraction

import pytest

from nexencoder.inject import (FRAME_RATES, SCENARIOS, build_ts, frame_pts, main,
                               run_scenario, verify_ts)
from nexencoder.scte35 import (PTS_MOD, SpliceInsert, SpliceNull, TimeSignal,
                               parse_section)

FPS = FRAME_RATES["29.97"]
PID = 0x1F0


def commands(events):
    return [parse_section(s).command for e in events for s in e.sections]


def test_frame_pts_exact_at_2997_and_wraps():
    assert frame_pts(1, FPS) == 3003
    assert frame_pts(30, FPS) == 90090
    assert frame_pts(1, Fraction(25)) == 3600
    assert frame_pts(0, FPS, start_pts=PTS_MOD - 1) == PTS_MOD - 1
    assert frame_pts(1, FPS, start_pts=PTS_MOD - 1) == (PTS_MOD - 1 + 3003) % PTS_MOD


def test_basic_scenario_places_splice_at_preroll():
    events = run_scenario(SCENARIOS["basic"](), FPS)
    start, end = commands(events)
    assert isinstance(start, SpliceInsert) and start.out_of_network
    assert start.pts_time == events[0].pts + 4000 * 90
    assert start.break_duration == 300 * 9000 and start.auto_return is True
    assert not end.out_of_network and end.pts_time == events[1].pts + 2000 * 90


def test_immediate_cancel_heartbeat_mixed():
    imm = commands(run_scenario(SCENARIOS["immediate"](), FPS))
    assert imm[0].immediate and imm[0].pts_time is None and imm[1].immediate
    canc = commands(run_scenario(SCENARIOS["cancel"](), FPS))
    assert canc[1] == SpliceInsert(event_id=300, cancel=True)
    hb = commands(run_scenario(SCENARIOS["heartbeat"](), FPS))
    assert hb == [SpliceNull()] * 6
    mixed = run_scenario(SCENARIOS["mixed"](), FPS)
    assert [len(e.sections) for e in mixed] == [1, 2, 0, 1]  # unknown op yields nothing
    assert isinstance(commands(mixed)[2], TimeSignal)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_scenario_survives_ts_round_trip(name):
    events = run_scenario(SCENARIOS[name](), FPS, start_pts=123456)
    data = build_ts(events, PID)
    assert len(data) % 188 == 0
    assert verify_ts(data, PID, events) == []


def test_verify_detects_corruption():
    events = run_scenario(SCENARIOS["basic"](), FPS)
    data = bytearray(build_ts(events, PID))
    data[20] ^= 0xFF
    assert verify_ts(bytes(data), PID, events) != []


def test_cli_writes_ts_and_log(tmp_path):
    out, log = tmp_path / "x.ts", tmp_path / "x.json"
    rc = main(["--scenario", "mixed", "--fps", "25", "--pid", "0x1F5",
               "--out", str(out), "--log", str(log)])
    assert rc == 0
    assert out.stat().st_size % 188 == 0 and out.stat().st_size > 0
    rows = json.loads(log.read_text())
    assert [len(r["scte35"]) for r in rows] == [1, 2, 0, 1]
    assert rows[1]["scte35"][0]["type"] == "SpliceInsert"
    assert bytes.fromhex(rows[0]["scte35_hex"][0])[0] == 0xFC


def test_cli_rejects_bad_arguments(tmp_path):
    for argv in (["--scenario", "nope", "--out", str(tmp_path / "a.ts")],
                 ["--pid", "0x0001", "--out", str(tmp_path / "a.ts")],
                 ["--fps", "12", "--out", str(tmp_path / "a.ts")]):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 2
