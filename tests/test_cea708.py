from helpers import make_packet, packet_to_triplets

from nexencoder.cea708 import Caption708Event, Cea708Decoder, Service708

# DF0: visible, anchor_v 0x70, anchor_h 0x20, 3 rows x 32 cols
DF0 = bytes([0x98, 0x20, 0x70, 0x20, 0x02, 0x1F, 0x01])


def lines_of(svc, data):
    ev = svc.feed(data)
    return None if ev is None else ev.lines


def test_text_appears_in_defined_window():
    svc = Service708(1)
    assert lines_of(svc, DF0 + b"HELLO") == ("HELLO",)


def test_cr_adds_line_and_scrolls_at_window_height():
    svc = Service708(1)
    svc.feed(DF0 + b"L1\rL2\rL3")
    assert svc.snapshot() == ("L1", "L2", "L3")
    svc.feed(b"\rL4")
    assert svc.snapshot() == ("L2", "L3", "L4")


def test_hide_show_clear():
    svc = Service708(1)
    svc.feed(DF0 + b"HI")
    assert lines_of(svc, bytes([0x8A, 0x01])) == ()
    assert lines_of(svc, bytes([0x89, 0x01])) == ("HI",)
    assert lines_of(svc, bytes([0x88, 0x01])) == ()


def test_toggle_and_delete():
    svc = Service708(1)
    svc.feed(DF0 + b"HI")
    assert lines_of(svc, bytes([0x8B, 0x01])) == ()
    assert lines_of(svc, bytes([0x8B, 0x01])) == ("HI",)
    assert lines_of(svc, bytes([0x8C, 0x01])) == ()
    assert svc.current is None


def test_command_split_across_blocks_is_reassembled():
    svc = Service708(1)
    assert svc.feed(DF0[:3]) is None
    assert lines_of(svc, DF0[3:] + b"OK") == ("OK",)


def test_special_characters_and_skipped_extended_codes():
    svc = Service708(1)
    svc.feed(DF0)
    assert lines_of(svc, b"\x7f") == ("♪",)
    assert lines_of(svc, b"\x10\x33") == ("♪“",)
    assert lines_of(svc, b"\x10\x08\x00A") == ("♪“A",)  # C2 with 1 param skipped
    assert lines_of(svc, b"\x10\x90\x02\xaa\xbbZ") == ("♪“AZ",)  # C3 variable length
    assert lines_of(svc, b"\xe9") == ("♪“AZé",)  # G1 Latin-1


def test_windows_ordered_by_vertical_anchor():
    svc = Service708(1)
    svc.feed(DF0 + b"BOTTOM")
    df1 = bytes([0x99, 0x20, 0x10, 0x20, 0x02, 0x1F, 0x01])
    assert lines_of(svc, df1 + b"TOP") == ("TOP", "BOTTOM")


def test_reset_clears_everything():
    svc = Service708(1)
    svc.feed(DF0 + b"HI")
    assert lines_of(svc, b"\x8f") == ()
    assert svc.windows == {}


def test_set_pen_location_and_backspace():
    svc = Service708(1)
    svc.feed(DF0 + bytes([0x92, 0x01, 0x03]) + b"X")
    assert svc.windows[0].cells[1][3] == "X"
    svc2 = Service708(1)
    assert lines_of(svc2, DF0 + b"AB\x08C") == ("AC",)


def test_no_event_when_visible_text_unchanged():
    svc = Service708(1)
    svc.feed(DF0 + b"HI")
    assert svc.feed(b"") is None
    assert svc.feed(bytes([0x8E])) is None  # delay-cancel has no visible effect


def test_redefine_same_size_preserves_text():
    svc = Service708(1)
    svc.feed(DF0 + b"KEEP")
    assert svc.feed(DF0) is None
    assert svc.snapshot() == ("KEEP",)


def test_decoder_end_to_end_from_triplets():
    dec = Cea708Decoder()
    packet = make_packet(0, [(1, DF0 + b"HI")])
    events = dec.feed(packet_to_triplets(packet))
    assert events == [Caption708Event(1, ("HI",))]


def test_bad_packet_counted_not_fatal():
    dec = Cea708Decoder()
    bad = bytes([2, (1 << 5) | 5, 0x41, 0x42])
    assert dec.feed(packet_to_triplets(bad)) == []
    assert dec.packet_errors == 1
    good = make_packet(1, [(1, DF0 + b"OK")])
    assert dec.feed(packet_to_triplets(good))[0].lines == ("OK",)
