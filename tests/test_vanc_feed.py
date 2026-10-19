"""VANC datagrams become one SCTE-35 insert and a caption note when the text changes."""

from helpers import make_packet, odd_parity, packet_to_triplets

from nexencoder import scte104 as s104
from nexencoder.cdp import CC_608_FIELD1, CcTriplet, build_cdp
from nexencoder.scte35 import SpliceNull, parse_section
from nexencoder.vanc_feed import KIND_CDP, KIND_SCTE, VancForwarder, pack_vanc_datagram, parse_vanc_datagram


def t608(b1, b2):
    return CcTriplet(True, CC_608_FIELD1, odd_parity(b1), odd_parity(b2))


def _cdp() -> bytes:
    triplets = [
        t608(0x14, 0x20), t608(0x14, 0x20),
        t608(0x14, 0x70), t608(0x14, 0x70),
        t608(ord("H"), ord("I")),
        t608(0x14, 0x2F), t608(0x14, 0x2F),
    ] + packet_to_triplets(make_packet(0, [(1, bytes([0x98, 0x20, 0x70, 0x20, 0x02, 0x1F, 0x01]) + b"HI")]))
    return build_cdp(4, 1, triplets)


def _mom() -> bytes:
    return s104.build_mom(s104.Mom(0, 0, 1, 0, 0, s104.TIME_NONE, b"", (s104.SpliceNull104(),)))


def test_datagram_round_trip():
    raw = pack_vanc_datagram(KIND_SCTE, 90000, _mom())
    kind, pts, payload = parse_vanc_datagram(raw)
    assert (kind, pts, payload) == (KIND_SCTE, 90000, _mom())


def test_scte104_is_inserted_once_until_it_ages_out():
    sent = []
    notes = []
    forwarder = VancForwarder("A", lambda section, port: sent.append((section, port)), notes.append)
    packet = pack_vanc_datagram(KIND_SCTE, 90000, _mom())
    forwarder.handle(packet, 0.0, False)
    assert sent == []
    forwarder.handle(packet, 1.0, True)
    forwarder.handle(packet, 2.0, True)
    assert len(sent) == 1
    section, port = sent[0]
    assert port == 19101
    assert isinstance(parse_section(section).command, SpliceNull)
    assert [note["kind"] for note in notes] == ["scte104"]
    forwarder.handle(packet, 40.0, True)
    assert len(sent) == 2


def test_scte104_is_forwarded_to_sdi_once():
    sent = []
    sdi = []
    forwarder = VancForwarder(
        "B",
        lambda section, port: sent.append((section, port)),
        lambda note: None,
        sdi_send=lambda payload, pts: sdi.append((payload, pts)),
    )
    packet = pack_vanc_datagram(KIND_SCTE, 180000, _mom())
    forwarder.handle(packet, 1.0, True)
    forwarder.handle(packet, 2.0, True)
    assert sdi == [(_mom(), 180000)]
    assert len(sent) == 1


def test_caption_notes_follow_text_changes():
    notes = []
    forwarder = VancForwarder("B", lambda section, port: None, notes.append)
    packet = pack_vanc_datagram(KIND_CDP, 0, _cdp())
    forwarder.handle(packet, 0.0, True)
    forwarder.handle(packet, 1.0, True)
    texts = [note["text"] for note in notes]
    assert texts.count("HI") == 2
    assert {note["channel"] for note in notes} == {"B"}
    assert all(note["kind"] == "caption" for note in notes)
