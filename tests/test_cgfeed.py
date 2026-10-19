"""CG slot files and feeder planning. Nothing here opens a DeckLink device."""

import struct
import zlib

from nexencoder.cgfeed import (
    SlotWriter,
    chromium_argv,
    decode_png_bgra,
    detect_html_backend,
    key_argv,
    ndi_argv,
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
    assert key[key.index("-i") + 1] == "DeckLink Duo (3)"
    assert key[key.index("-i", key.index("-i") + 1) + 1] == "DeckLink Duo (4)"
    assert key.count("-i") == 2
    assert "alphamerge" in " ".join(key)
    assert "yuv422p10" not in key


def _png(width: int, height: int, rgba: bytes) -> bytes:
    assert len(rgba) == width * height * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw += rgba[y * width * 4:(y + 1) * width * 4]
    compressed = zlib.compress(bytes(raw))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")


def test_png_alpha_becomes_straight_bgra():
    png = _png(2, 1, bytes([0, 255, 0, 128, 255, 0, 0, 0]))
    bgra = decode_png_bgra(png)
    assert bgra == bytes([0, 255, 0, 128, 0, 0, 255, 0])


def test_chromium_capture_keeps_a_transparent_background():
    argv = chromium_argv("chromium", 9222, "http://127.0.0.1:8788/cg/bug.html", 1280, 720)
    assert "--default-background-color=00000000" in argv
    assert "--no-sandbox" in argv
    assert "--user-data-dir=/var/lib/nexencoder/chrome/9222" in argv
    assert "jpeg" not in " ".join(argv)


def test_ndi_argv_names_the_source_and_requests_bgra():
    argv = ndi_argv("GRAPHICS (NDI)", 1920, 1080)
    assert "ndisrc" in argv
    assert 'ndi-name="GRAPHICS (NDI)"' in argv
    assert "BGRA" in "".join(argv)
    ndi = {"slot": 0, "source": {"type": "ndi", "name": "GRAPHICS (NDI)"}}
    from nexencoder.cgfeed import feeder_key
    assert feeder_key(ndi) == "ndi:0:GRAPHICS (NDI)"


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
    padded = "OBJS-$(CONFIG_COPY_FILTER)                   += vf_copy.o\n"
    aligned = patch_makefile(padded)
    assert aligned.startswith(padded)
    assert "CONFIG_NEXTONE_FILTER" in aligned
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
