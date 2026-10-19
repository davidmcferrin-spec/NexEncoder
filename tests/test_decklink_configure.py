"""DeckLink connector mapping. Nothing here opens a card."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_configure_source_prefers_half_duplex_inputs():
    text = (ROOT / "decklink-configure.cpp").read_text(encoding="utf-8")
    start = text.index("const BMDProfileID candidates[]")
    chunk = text[start:text.index("};", start)]
    two = chunk.index("bmdProfileTwoSubDevicesHalfDuplex")
    four = chunk.index("bmdProfileFourSubDevicesHalfDuplex")
    one = chunk.index("bmdProfileOneSubDeviceHalfDuplex")
    assert two < four < one
    assert "--apply-inputs" in text
    assert "nexencoder-channel@A" in text
    assert "SetActive" in text


def test_configure_unit_runs_once_before_the_channels():
    text = (ROOT / "systemd" / "nexencoder-decklink-configure.service").read_text(encoding="utf-8")
    assert "Type=oneshot" in text
    assert "RemainAfterExit=yes" in text
    assert "Before=nexencoder-channel@A.service nexencoder-channel@B.service" in text
    assert "/usr/local/bin/decklink-configure --apply-inputs" in text
    assert '[ "$rc" -eq 1 ] && exit 0' in text
    assert "Restart=" not in text


def test_setup_builds_the_tool_and_applies_it_before_the_channels():
    script = (ROOT / "setup.sh").read_text(encoding="utf-8")
    assert script.index("install_decklink\n") < script.index("install_decklink_configure\n")
    assert "make DECKLINK_SDK=" in script
    assert "/usr/local/bin/decklink-configure" in script
    assert "/usr/local/bin/decklink-status" in script
    assert "input_locked" in (ROOT / "decklink-status.cpp").read_text(encoding="utf-8")
    assert "nexencoder-decklink-configure.service" in script
    stop = script.index("systemctl stop nexencoder-channel@A nexencoder-channel@B")
    start = script.index("systemctl start nexencoder-decklink-configure")
    restart = script.index("systemctl restart nexencoder-channel@A nexencoder-channel@B")
    assert stop < start < restart
