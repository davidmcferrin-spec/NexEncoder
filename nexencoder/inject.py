"""SCTE-104 injector: build realistic messages, run them through the production
parse + translate path, and write the resulting SCTE-35 sections into a TS file.

Every message is serialized to SCTE-104 bytes and re-parsed before translation, so the
injector exercises exactly what a capture feeding live VANC would. Use it to produce
test streams for TSDuck/ffprobe/downstream decoders and as a loopback self-check.

    python3 -m nexencoder.inject --scenario basic --fps 29.97 --out basic.ts --log basic.json
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from dataclasses import dataclass
from fractions import Fraction
from typing import Callable, Optional

from . import scte104 as s104
from .errors import ParseError
from .scte35 import PTS_MOD, parse_section
from .translate import translate_mom
from .ts import SectionPacketizer, extract_sections

log = logging.getLogger("nexencoder.inject")

FRAME_RATES = {
    "23.976": Fraction(24000, 1001), "24": Fraction(24), "25": Fraction(25),
    "29.97": Fraction(30000, 1001), "30": Fraction(30), "50": Fraction(50),
    "59.94": Fraction(60000, 1001), "60": Fraction(60),
}


@dataclass(frozen=True)
class Scheduled:
    """A SCTE-104 operation set to send ``at`` seconds into the scenario."""

    at: float
    operations: tuple[s104.Operation, ...]


@dataclass(frozen=True)
class Injected:
    frame: int
    pts: int
    mom: bytes
    sections: tuple[bytes, ...]


def _req(kind: int, event_id: int, pre_roll_ms: int = 4000, dur_ds: int = 0,
         auto_return: bool = False, upid: int = 1) -> s104.SpliceRequest:
    return s104.SpliceRequest(kind, event_id, upid, pre_roll_ms, dur_ds, 1, 1, auto_return)


def _basic() -> list[Scheduled]:
    return [
        Scheduled(5.0, (_req(s104.SPLICE_START_NORMAL, 100, 4000, 300, True),)),
        Scheduled(40.0, (_req(s104.SPLICE_END_NORMAL, 100, 2000),)),
    ]


def _immediate() -> list[Scheduled]:
    return [
        Scheduled(3.0, (_req(s104.SPLICE_START_IMMEDIATE, 200, 0, 600, False),)),
        Scheduled(20.0, (_req(s104.SPLICE_END_IMMEDIATE, 200, 0),)),
    ]


def _cancel() -> list[Scheduled]:
    return [
        Scheduled(2.0, (_req(s104.SPLICE_START_NORMAL, 300, 8000, 450, True),)),
        Scheduled(6.0, (_req(s104.SPLICE_CANCEL, 300),)),
    ]


def _heartbeat() -> list[Scheduled]:
    return [Scheduled(float(t), (s104.SpliceNull104(),)) for t in range(0, 60, 10)]


def _mixed() -> list[Scheduled]:
    return [
        Scheduled(1.0, (s104.SpliceNull104(),)),
        Scheduled(5.0, (_req(s104.SPLICE_START_NORMAL, 400, 5000, 600, True),
                        s104.TimeSignalRequest(5000))),
        Scheduled(10.0, (s104.UnknownOp(0x0109, b"\x01\x02\x03"),)),  # no 35 mapping
        Scheduled(65.0, (_req(s104.SPLICE_END_NORMAL, 400, 2000),)),
    ]


SCENARIOS: dict[str, Callable[[], list[Scheduled]]] = {
    "basic": _basic, "immediate": _immediate, "cancel": _cancel,
    "heartbeat": _heartbeat, "mixed": _mixed,
}


def frame_pts(frame: int, fps: Fraction, start_pts: int = 0) -> int:
    """90 kHz PTS of ``frame`` at ``fps``, wrapped to 33 bits."""
    return (start_pts + int(Fraction(frame * 90000) / fps)) % PTS_MOD


def run_scenario(schedule: list[Scheduled], fps: Fraction, start_pts: int = 0) -> list[Injected]:
    """Serialize, re-parse, and translate each scheduled message."""
    out: list[Injected] = []
    for number, item in enumerate(sorted(schedule, key=lambda s: s.at)):
        frame = round(item.at * float(fps))
        pts = frame_pts(frame, fps, start_pts)
        mom = s104.build_mom(s104.Mom(0, 0, number & 0xFF, 0, 0, s104.TIME_NONE, b"",
                                      item.operations))
        sections = tuple(translate_mom(s104.parse_mom(mom), pts))
        out.append(Injected(frame, pts, mom, sections))
    return out


def build_ts(events: list[Injected], pid: int) -> bytes:
    """Packetize every section, in event order, onto ``pid``."""
    pk = SectionPacketizer(pid)
    return b"".join(p for e in events for s in e.sections for p in pk.packetize(s))


def verify_ts(data: bytes, pid: int, events: list[Injected]) -> list[str]:
    """Extract sections back out of the TS and compare with what was injected."""
    expected = [s for e in events for s in e.sections]
    got = extract_sections(data, pid)
    problems: list[str] = []
    if len(got) != len(expected):
        problems.append(f"expected {len(expected)} sections, extracted {len(got)}")
    for i, (g, e) in enumerate(zip(got, expected)):
        if g != e:
            problems.append(f"section {i} differs from the injected bytes")
            continue
        try:
            parse_section(g)
        except ParseError as exc:
            problems.append(f"section {i} fails to parse: {exc}")
    return problems


def event_log(events: list[Injected], fps: Fraction) -> list[dict]:
    rows = []
    for e in events:
        cmds = []
        for s in e.sections:
            cmd = parse_section(s).command
            cmds.append({"type": type(cmd).__name__, **dataclasses.asdict(cmd)})
        rows.append({"frame": e.frame, "seconds": float(e.frame / fps), "pts": e.pts,
                     "scte104_hex": e.mom.hex(), "scte35": cmds,
                     "scte35_hex": [s.hex() for s in e.sections]})
    return rows


def _pid(text: str) -> int:
    value = int(text, 0)
    if not 0x0010 <= value <= 0x1FFE:
        raise argparse.ArgumentTypeError("PID must be between 0x0010 and 0x1FFE")
    return value


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="nexencoder.inject", description=__doc__.split("\n\n")[0])
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="basic")
    ap.add_argument("--fps", choices=sorted(FRAME_RATES, key=float), default="29.97")
    ap.add_argument("--pid", type=_pid, default=0x1F0, help="SCTE-35 PID (default 0x1F0)")
    ap.add_argument("--start-pts", type=int, default=0, help="PTS of frame 0 (90 kHz)")
    ap.add_argument("--out", required=True, help="output .ts path")
    ap.add_argument("--log", help="optional JSON event log path")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")

    fps = FRAME_RATES[args.fps]
    events = run_scenario(SCENARIOS[args.scenario](), fps, args.start_pts % PTS_MOD)
    data = build_ts(events, args.pid)
    problems = verify_ts(data, args.pid, events)
    if problems:
        for p in problems:
            log.error("self-check failed: %s", p)
        return 1
    with open(args.out, "wb") as fh:
        fh.write(data)
    if args.log:
        with open(args.log, "w", encoding="utf-8") as fh:
            json.dump(event_log(events, fps), fh, indent=2)
    log.info("wrote %s: %d messages, %d sections, %d packets (self-check OK)", args.out,
             len(events), sum(len(e.sections) for e in events), len(data) // 188)
    return 0


if __name__ == "__main__":
    sys.exit(main())
