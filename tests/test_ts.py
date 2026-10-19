import pytest

from nexencoder.errors import ParseError
from nexencoder.scte35 import SpliceInsert, build_section, parse_section
from nexencoder.ts import (PreviewGate, SectionExtractor, SectionPacketizer,
                           TS_PACKET, extract_sections, iter_packets)

PID = 0x1F0


def fake_section(n: int, fill: int = 0xAB) -> bytes:
    """A syntactically framed section of total length n (3-byte header + body)."""
    body_len = n - 3
    return bytes([0xFC, 0x30 | (body_len >> 8), body_len & 0xFF]) + bytes([fill]) * body_len


def test_single_section_round_trip():
    sec = build_section(SpliceInsert(event_id=1, out_of_network=True, pts_time=900000,
                                     break_duration=2700000, auto_return=True))
    pk = SectionPacketizer(PID)
    packets = pk.packetize(sec)
    assert len(packets) == 1 and all(len(p) == TS_PACKET for p in packets)
    data = b"".join(packets)
    assert extract_sections(data, PID) == [sec]
    assert parse_section(extract_sections(data, PID)[0]).command.event_id == 1


def test_section_spanning_multiple_packets():
    sec = fake_section(500)
    packets = SectionPacketizer(PID).packetize(sec)
    assert len(packets) == 3
    assert packets[0][1] & 0x40 and not packets[1][1] & 0x40
    assert extract_sections(b"".join(packets), PID) == [sec]


def test_continuity_counter_increments_and_wraps():
    pk = SectionPacketizer(PID)
    ccs = []
    for _ in range(20):
        ccs.append(pk.packetize(fake_section(10))[0][3] & 0x0F)
    assert ccs == [i % 16 for i in range(20)]


def test_many_sections_in_order():
    pk = SectionPacketizer(PID)
    secs = [fake_section(20 + i * 50, fill=i) for i in range(8)]
    data = b"".join(b"".join(pk.packetize(s)) for s in secs)
    assert extract_sections(data, PID) == secs


def test_other_pids_ignored():
    a, b = SectionPacketizer(PID), SectionPacketizer(0x1F1)
    sa, sb = fake_section(30, 1), fake_section(30, 2)
    data = b"".join(a.packetize(sa) + b.packetize(sb))
    assert extract_sections(data, PID) == [sa]
    assert extract_sections(data, 0x1F1) == [sb]


def test_continuity_error_discards_partial_and_recovers():
    pk = SectionPacketizer(PID)
    big = pk.packetize(fake_section(500))
    good = fake_section(40, 9)
    good_pkts = pk.packetize(good)
    ex = SectionExtractor(PID)
    out = []
    for p in [big[0], big[2]] + good_pkts:  # middle packet lost
        out += ex.feed(p)
    assert out == [good]
    assert ex.cc_errors == 1


def test_duplicate_packet_ignored():
    pkts = SectionPacketizer(PID).packetize(fake_section(30))
    ex = SectionExtractor(PID)
    out = ex.feed(pkts[0]) + ex.feed(pkts[0])
    assert len(out) == 1 and ex.duplicates == 1


def test_pointer_field_with_section_tail_and_new_start():
    first, second = fake_section(250, 1), fake_section(60, 2)
    stream = first + second
    # packet 1 carries the first 183 bytes of `first`; packet 2 carries the tail of
    # `first` (67 bytes) via pointer_field, then `second` starts in the same packet.
    p1 = bytes([0x47, 0x40 | (PID >> 8), PID & 0xFF, 0x10]) + b"\x00" + stream[:183]
    tail = stream[183:250]
    body = stream[250:]
    payload = bytes([len(tail)]) + tail + body
    p2 = bytes([0x47, 0x40 | (PID >> 8), PID & 0xFF, 0x11]) + payload
    p2 += b"\xff" * (TS_PACKET - len(p2))
    assert extract_sections(p1 + p2, PID) == [first, second]


def test_adaptation_field_skipped():
    sec = fake_section(30, 7)
    af = bytes([3, 0x00, 0xFF, 0xFF])  # length 3 + 3 bytes
    payload = b"\x00" + sec
    pkt = bytes([0x47, 0x40 | (PID >> 8), PID & 0xFF, 0x30]) + af + payload
    pkt += b"\xff" * (TS_PACKET - len(pkt))
    assert extract_sections(pkt, PID) == [sec]


# PAT program 1 -> PMT 0x100. PMT: H.264 on 0x101, AAC on 0x102.
_PAT = bytes([
    0x00, 0xB0, 0x0D, 0x00, 0x01, 0xC1, 0x00, 0x00,
    0x00, 0x01, 0xE1, 0x00, 0x00, 0x00, 0x00, 0x00,
])
_PMT = bytes([
    0x02, 0xB0, 0x17, 0x00, 0x01, 0xC1, 0x00, 0x00,
    0xE1, 0x01, 0xF0, 0x00,
    0x1B, 0xE1, 0x01, 0xF0, 0x00,
    0x0F, 0xE1, 0x02, 0xF0, 0x00,
    0x00, 0x00, 0x00, 0x00,
])


def _packet(pid: int, payload: bytes, *, pusi: bool = True, rai: bool = False, cc: int = 0) -> bytes:
    body = (bytes([1, 0x40]) + payload) if rai else payload
    raw = bytes([
        0x47,
        (0x40 if pusi else 0) | ((pid >> 8) & 0x1F),
        pid & 0xFF,
        (0x30 if rai else 0x10) | (cc & 0x0F),
    ]) + body
    assert len(raw) <= TS_PACKET
    return raw + bytes(TS_PACKET - len(raw))


def test_preview_gate_starts_at_the_video_keyframe():
    pat = _packet(0, b"\x00" + _PAT)
    pmt = _packet(0x100, b"\x00" + _PMT)
    early = _packet(0x101, b"\x00\x00\x01\x41" + b"\x11" * 16, cc=1)
    audio = _packet(0x102, b"\x22" * 16, rai=True)
    key = _packet(0x101, b"\x00\x00\x00\x01\x67\x42" + b"\x33" * 16, rai=True, cc=2)
    later = _packet(0x101, b"\x44" * 16, pusi=False, cc=3)
    gate = PreviewGate()
    assert gate.feed(early + audio) == b""
    assert gate.opened is False
    # A keyframe before the PMT is dropped; its continuation is already gone.
    assert gate.feed(pat + key) == b""
    out = gate.feed(pmt + early + audio + key + later)
    assert out == pat + pmt + key + later
    assert gate.opened is True
    nxt = _packet(0x101, b"\x55" * 16, pusi=False, cc=4)
    assert gate.feed(nxt) == nxt


def test_preview_gate_accepts_an_sps_without_the_random_access_bit():
    pat = _packet(0, b"\x00" + _PAT)
    pmt = _packet(0x100, b"\x00" + _PMT)
    key = _packet(0x101, b"\x00\x00\x00\x01\x67\x64" + b"\x66" * 8)
    gate = PreviewGate()
    assert gate.feed(pat + pmt + key) == pat + pmt + key


def test_preview_gate_waits_for_a_whole_packet():
    pat = _packet(0, b"\x00" + _PAT)
    pmt = _packet(0x100, b"\x00" + _PMT)
    key = _packet(0x101, b"\x00\x00\x00\x01\x67\x42" + b"\x77" * 8, rai=True)
    blob = pat + pmt + key
    gate = PreviewGate()
    assert gate.feed(b"\xff" + blob[:40]) == b""
    assert gate.feed(blob[40:]) == pat + pmt + key


def test_bad_input_rejected():
    with pytest.raises(ParseError):
        list(iter_packets(b"\x47" * 100))
    with pytest.raises(ParseError):
        list(iter_packets(b"\x00" * TS_PACKET))
    with pytest.raises(ValueError):
        SectionPacketizer(0x0001)
    with pytest.raises(ValueError):
        SectionPacketizer(PID).packetize(b"")
