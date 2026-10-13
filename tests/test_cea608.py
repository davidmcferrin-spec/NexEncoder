from nexencoder.cea608 import Cea608Decoder


def p(b: int) -> int:
    """Apply odd parity to a 7-bit value."""
    return b | (0x80 if bin(b).count("1") % 2 == 0 else 0)


def feed(dec, field, b1, b2):
    return dec.feed(field, p(b1), p(b2))


def ctrl(dec, field, b1, b2, twice=True):
    out = feed(dec, field, b1, b2)
    if twice:
        out += feed(dec, field, b1, b2)
    return out


def text(dec, field, s):
    out = []
    s = s + ("\x00" if len(s) % 2 else "")
    for i in range(0, len(s), 2):
        out += feed(dec, field, ord(s[i]), ord(s[i + 1]))
    return out


def test_pop_on_displays_only_on_eoc():
    d = Cea608Decoder()
    ctrl(d, 1, 0x14, 0x20)  # RCL
    ctrl(d, 1, 0x14, 0x70)  # PAC row 15
    assert text(d, 1, "HELLO") == []
    events = ctrl(d, 1, 0x14, 0x2F)  # EOC sent twice
    assert len(events) == 1
    assert events[0].channel == 1 and events[0].mode == "popon"
    assert events[0].lines == ("HELLO",)


def test_edm_clears_display():
    d = Cea608Decoder()
    ctrl(d, 1, 0x14, 0x20)
    ctrl(d, 1, 0x14, 0x70)
    text(d, 1, "HI")
    ctrl(d, 1, 0x14, 0x2F)
    events = ctrl(d, 1, 0x14, 0x2C)
    assert len(events) == 1 and events[0].lines == ()


def test_roll_up_scrolls():
    d = Cea608Decoder()
    ctrl(d, 1, 0x14, 0x25)  # RU2
    ctrl(d, 1, 0x14, 0x70)
    text(d, 1, "AB")
    ctrl(d, 1, 0x14, 0x2D)  # CR
    events = text(d, 1, "CD")
    assert events[-1].lines == ("AB", "CD")
    ctrl(d, 1, 0x14, 0x2D)
    events = text(d, 1, "EF")
    assert events[-1].lines == ("CD", "EF")


def test_paint_on_writes_immediately():
    d = Cea608Decoder()
    ctrl(d, 1, 0x14, 0x29)  # RDC
    ctrl(d, 1, 0x14, 0x70)
    events = text(d, 1, "ON")
    assert events[-1].lines == ("ON",) and events[-1].mode == "painton"


def test_parity_error_dropped_and_counted():
    d = Cea608Decoder()
    ctrl(d, 1, 0x14, 0x29)
    ctrl(d, 1, 0x14, 0x70)
    bad = p(ord("A")) ^ 0x80
    assert d.feed(1, bad, p(ord("B"))) == []
    assert d.parity_errors == 1


def test_field2_uses_cc3_and_cc4():
    d = Cea608Decoder()
    ctrl(d, 2, 0x15, 0x29)  # RDC, ch1 of field 2 -> CC3
    ctrl(d, 2, 0x15, 0x70)
    events = text(d, 2, "F2")
    assert events[-1].channel == 3
    ctrl(d, 2, 0x1D, 0x29)  # CC4
    ctrl(d, 2, 0x1D, 0x70)
    events = text(d, 2, "F4")
    assert events[-1].channel == 4


def test_special_extended_and_basic_substitutions():
    d = Cea608Decoder()
    ctrl(d, 1, 0x14, 0x29)
    ctrl(d, 1, 0x14, 0x70)
    feed(d, 1, 0x11, 0x37)  # music note
    feed(d, 1, ord("E"), 0)
    events = feed(d, 1, 0x12, 0x21)  # extended E-acute replaces previous char
    assert events[-1].lines == ("♪É",)
    events = feed(d, 1, 0x2A, 0)  # basic substitution: 0x2A -> a-acute
    assert events[-1].lines == ("♪Éá",)


def test_pac_indent_and_tab_offset():
    d = Cea608Decoder()
    ctrl(d, 1, 0x14, 0x29)
    ctrl(d, 1, 0x14, 0x52)  # row 14, indent bit set -> indent 4
    events = text(d, 1, "X")
    assert events[-1].lines == ("X",)
    assert d._channels[1].displayed[14][4] == "X"


def test_text_mode_ignored():
    d = Cea608Decoder()
    ctrl(d, 1, 0x14, 0x2A)  # TR
    assert text(d, 1, "TEXT") == []


def test_duplicate_control_suppressed_only_when_consecutive():
    d = Cea608Decoder()
    ctrl(d, 1, 0x14, 0x25, twice=False)  # RU2
    ctrl(d, 1, 0x14, 0x70, twice=False)
    text(d, 1, "A\x00")
    ctrl(d, 1, 0x14, 0x2D, twice=False)  # CR
    text(d, 1, "B\x00")  # data between resets suppression
    # Second CR is a new command (data intervened), so RU2 scrolls "A" off the top.
    events = ctrl(d, 1, 0x14, 0x2D, twice=False)
    assert events and events[-1].lines == ("B",)
