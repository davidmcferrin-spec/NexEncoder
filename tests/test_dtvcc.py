import pytest
from helpers import make_packet, packet_to_triplets

from nexencoder.cdp import CC_608_FIELD1, CC_DTVCC_DATA, CC_DTVCC_START, CcTriplet
from nexencoder.dtvcc import DtvccAssembler, split_service_blocks
from nexencoder.errors import ParseError


def test_assemble_and_split_single_service():
    pkt = make_packet(0, [(1, b"\x41\x42\x43")])
    asm = DtvccAssembler()
    done = []
    for t in packet_to_triplets(pkt):
        done += asm.feed(t)
    assert done == [pkt]
    assert list(split_service_blocks(done[0])) == [(1, b"\x41\x42\x43")]


def test_two_services_in_one_packet():
    pkt = make_packet(1, [(1, b"AB"), (2, b"CDE")])
    assert list(split_service_blocks(pkt)) == [(1, b"AB"), (2, b"CDE")]


def test_extended_service_number():
    body = bytes([(7 << 5) | 2, 12]) + b"HI"
    pkt = bytes([(len(body) + 2) // 2]) + body + b"\x00"
    assert list(split_service_blocks(pkt)) == [(12, b"HI")]


def test_start_mid_packet_drops_previous():
    asm = DtvccAssembler()
    ts = packet_to_triplets(make_packet(0, [(1, b"A" * 10)]))
    asm.feed(ts[0])
    asm.feed(ts[1])
    short = make_packet(1, [(1, b"Z")])
    out = []
    for t in packet_to_triplets(short):
        out += asm.feed(t)
    assert out == [short]
    assert asm.dropped_packets == 1


def test_orphan_continuation_and_invalid_ignored():
    asm = DtvccAssembler()
    assert asm.feed(CcTriplet(True, CC_DTVCC_DATA, 1, 2)) == []
    assert asm.orphan_bytes == 2
    assert asm.feed(CcTriplet(False, CC_DTVCC_START, 1, 2)) == []
    assert asm.feed(CcTriplet(True, CC_608_FIELD1, 1, 2)) == []


def test_sequence_gap_counted():
    asm = DtvccAssembler()
    for seq in (0, 2):
        for t in packet_to_triplets(make_packet(seq, [(1, b"A")])):
            asm.feed(t)
    assert asm.sequence_errors == 1


def test_block_overrun_raises():
    pkt = bytes([2, (1 << 5) | 5, 0x41, 0x42])
    with pytest.raises(ParseError):
        list(split_service_blocks(pkt))
