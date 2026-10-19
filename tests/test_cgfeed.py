"""CG slot files and feeder planning. Nothing here opens a DeckLink device."""

import io
import json
import logging
import struct
import threading
import zlib

from nexencoder.cgfeed import (
    SlotWriter,
    _Running,
    _drain,
    chromium_argv,
    chromium_env,
    decode_png_bgra,
    devtools_page_url,
    finished_keys,
    reconcile_once,
    with_lower_priority,
    detect_html_backend,
    feeder_key,
    key_argv,
    ndi_argv,
    plan_changes,
    read_slot,
    update_key_owner,
    wpe_argv,
)
from nexencoder.effects import layers_path
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
    assert "--disable-background-networking" in argv
    assert "--disable-domain-reliability" in argv
    assert any("PushMessaging" in item for item in argv)
    assert "--no-sandbox" in argv
    assert "--remote-allow-origins=*" in argv
    assert "--user-data-dir=/var/lib/nexencoder/chrome/9222" in argv
    assert "jpeg" not in " ".join(argv)


def test_devtools_page_url_ignores_the_browser_socket():
    browser = {
        "type": "browser",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/abc",
    }
    hidden = {"type": "page", "id": "1"}
    page = {
        "type": "page",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/def",
    }
    assert devtools_page_url([browser, hidden]) == ""
    assert devtools_page_url([browser, page]) == page["webSocketDebuggerUrl"]


def test_chromium_env_has_no_session_bus():
    env = chromium_env({"DBUS_SESSION_BUS_ADDRESS": "disabled:", "PATH": "/usr/bin"})
    assert "DBUS_SESSION_BUS_ADDRESS" not in env
    assert env["PATH"] == "/usr/bin"


def test_chrome_bus_errors_stay_out_of_the_journal(caplog):
    caplog.set_level(logging.WARNING)
    _drain(io.BytesIO(
        b"Failed to connect to the bus: Could not parse server address\n"
        b"Failed to call method: org.freedesktop.DBus.NameHasOwner\n"
        b"DevTools listening on ws://127.0.0.1:9/devtools/browser/x\n"
    ), "chromium")
    assert [record.message for record in caplog.records] == [
        "chromium DevTools listening on ws://127.0.0.1:9/devtools/browser/x",
    ]


def test_a_stopped_html_feeder_starts_again(tmp_path, monkeypatch):
    url = "http://127.0.0.1/cg/bug.html"
    spec = {
        "width": 320,
        "height": 180,
        "layers": [{
            "slot": 0,
            "path": str(tmp_path / "a.nxcg"),
            "source": {"type": "html", "url": url},
        }],
    }
    layers_path(tmp_path, "A").write_text(json.dumps(spec), encoding="utf-8")
    started = []
    hold = threading.Event()

    def fake_start(layer, spec, backend, spawn):
        started.append(feeder_key(layer))
        holder = _Running()
        holder.thread = threading.Thread(target=hold.wait, daemon=True)
        holder.thread.start()
        return holder

    monkeypatch.setattr("nexencoder.cgfeed._start_layer", fake_start)
    dead = _Running()
    dead.thread = threading.Thread(target=lambda: None)
    dead.thread.start()
    dead.thread.join()
    key = f"html:0:{url}"
    assert finished_keys({key: dead}) == [key]
    halted = _Running()
    halted.stop.set()
    halted.thread = dead.thread
    assert finished_keys({key: halted}) == []
    out = reconcile_once(tmp_path, "A", {key: dead}, backend="chromium")
    assert started == [key]
    assert out[key] is not dead
    assert out[key].thread.is_alive()


def test_html_renderer_runs_below_the_capture():
    argv = chromium_argv("google-chrome", 9223, "http://127.0.0.1/cg/bug.html", 1920, 1080)
    demoted = with_lower_priority(argv, demote=True)
    assert demoted[:3] == ["/usr/bin/nice", "-n", "10"]
    assert demoted[3] == "google-chrome"
    assert with_lower_priority(argv, demote=False)[0] == "google-chrome"


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
