"""CG slot files and feeder planning. Nothing here opens a DeckLink device."""

from nexencoder.cgfeed import (
    SlotWriter,
    detect_html_backend,
    key_argv,
    plan_changes,
    read_slot,
    update_key_owner,
    wpe_argv,
)
from nexencoder.effect_patch import MARKER, filter_source, patch_allfilters, patch_makefile


def test_slot_seqlock_roundtrip(tmp_path):
    writer = SlotWriter(tmp_path / "layer.nxcg", 4, 2)
    pixels = bytes([index % 256 for index in range(4 * 2 * 4)])
    assert read_slot(writer.path, 4, 2) is None
    writer.write(pixels)
    assert read_slot(writer.path, 4, 2) == pixels
    assert writer.seq % 2 == 0


def test_feeder_plan_restarts_only_the_source_that_changed():
    html = {"slot": 0, "source": {"type": "html", "url": "https://cg.example/a"}}
    key = {"slot": 1, "source": {"type": "sdi-key"}}
    stop, start = plan_changes({}, [html, key])
    assert stop == []
    assert start == [html, key]
    running = {"html:0:https://cg.example/a": html, "sdi": key}
    moved = dict(html)
    stop, start = plan_changes(running, [moved, key])
    assert stop == []
    assert start == []
    changed = {"slot": 0, "source": {"type": "html", "url": "https://cg.example/b"}}
    stop, start = plan_changes(running, [changed])
    assert "sdi" in stop
    assert start == [changed]


def test_key_and_html_argv_do_not_open_the_program_input():
    html = wpe_argv("https://cg.example/bug", 1920, 1080)
    assert html[0] == "gst-launch-1.0"
    assert "wpesrc" in html
    assert "BGRA" in "".join(html)
    assert "DeckLink" not in " ".join(html)
    key = key_argv("Hi59")
    assert key[key.index("-i") + 1] == "DeckLink Duo 2 (3)"
    assert key[key.index("-i", key.index("-i") + 1) + 1] == "DeckLink Duo 2 (4)"
    assert key.count("-i") == 2
    assert "alphamerge" in " ".join(key)
    assert "yuv422p10" not in key


def test_html_backend_prefers_wpe_then_chromium():
    assert detect_html_backend(which=lambda _name: None, inspect=lambda: False) == "none"
    assert detect_html_backend(which=lambda name: "/usr/bin/gst" if name == "gst-launch-1.0" else None, inspect=lambda: True) == "wpe"
    assert detect_html_backend(
        which=lambda name: "/usr/bin/chromium" if name == "chromium" else None,
        inspect=lambda: False,
    ) == "chromium"


def test_one_channel_owns_the_shared_key(tmp_path):
    assert update_key_owner(tmp_path, "B", True) == "B"
    assert update_key_owner(tmp_path, "A", True) == "A"
    assert update_key_owner(tmp_path, "A", False) == "B"
    assert update_key_owner(tmp_path, "B", False) == ""


def test_filter_patch_registers_nexeffect_and_copies_side_data(tmp_path):
    source = filter_source()
    assert MARKER in source
    assert "av_frame_copy_props" in source
    assert "squeeze_fields" in source
    makefile = patch_makefile("OBJS-$(CONFIG_COPY_FILTER) += vf_copy.o\n")
    assert "CONFIG_NEXEFFECT_FILTER" in makefile
    assert patch_makefile(makefile) == makefile
    filters = patch_allfilters("extern const FFFilter ff_vf_perspective;\n")
    assert "ff_vf_nexeffect" in filters
    assert "ff_af_nextone" in filters
    assert filters.index("ff_vf_nexeffect") < filters.index("ff_vf_perspective")
    (tmp_path / "libavfilter").mkdir()
    (tmp_path / "libavfilter" / "Makefile").write_text(makefile, encoding="utf-8")
    (tmp_path / "libavfilter" / "allfilters.c").write_text(filters, encoding="utf-8")
    from nexencoder.effect_patch import install_filter
    install_filter(tmp_path)
    assert "nexeffect" in (tmp_path / "libavfilter" / "vf_nexeffect.c").read_text(encoding="utf-8")
    tone = (tmp_path / "libavfilter" / "af_nextone.c").read_text(encoding="utf-8")
    assert "nexencoder tone" in tone
    assert "WAITING FOR INPUT" in (tmp_path / "libavfilter" / "vf_nexeffect.c").read_text(encoding="utf-8")
