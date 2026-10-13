"""Install the nexeffect filter into an FFmpeg 9.0.2 source tree.

setup.sh runs this after unpacking the pinned tarball and before configure.
The filter source lives next to this module so the lerp and the composite
order stay in the repo, not only inside a downloaded tree.
"""

from __future__ import annotations

from pathlib import Path

MARKER = "nexencoder effect stack"
MAKE_ANCHOR = "OBJS-$(CONFIG_COPY_FILTER) += vf_copy.o\n"
MAKE_LINE = (
    "OBJS-$(CONFIG_NEXEFFECT_FILTER) += vf_nexeffect.o\n"
    "OBJS-$(CONFIG_NEXTONE_FILTER) += af_nextone.o\n"
)
ALL_ANCHOR = "extern const FFFilter ff_vf_perspective;\n"
ALL_LINE = "extern const FFFilter ff_af_nextone;\nextern const FFFilter ff_vf_nexeffect;\n"


def filter_source() -> str:
    return Path(__file__).with_name("vf_nexeffect.c").read_text(encoding="utf-8")


def tone_source() -> str:
    return Path(__file__).with_name("af_nextone.c").read_text(encoding="utf-8")


def patch_makefile(text: str) -> str:
    if "CONFIG_NEXEFFECT_FILTER" in text:
        return text
    if MAKE_ANCHOR not in text:
        raise ValueError("libavfilter Makefile has no copy filter anchor")
    return text.replace(MAKE_ANCHOR, MAKE_ANCHOR + MAKE_LINE, 1)


def patch_allfilters(text: str) -> str:
    if "ff_vf_nexeffect" in text:
        return text
    if ALL_ANCHOR not in text:
        raise ValueError("allfilters.c has no perspective declaration to anchor on")
    return text.replace(ALL_ANCHOR, ALL_LINE + ALL_ANCHOR, 1)


def install_filter(src_root: str | Path) -> None:
    root = Path(src_root)
    source = filter_source()
    tone = tone_source()
    if MARKER not in source:
        raise ValueError("nexeffect source is missing its marker")
    if "nexencoder tone" not in tone:
        raise ValueError("nextone source is missing its marker")
    dest_dir = root / "libavfilter"
    if not dest_dir.is_dir():
        raise ValueError("ffmpeg source has no libavfilter directory")
    (dest_dir / "vf_nexeffect.c").write_text(source, encoding="utf-8")
    (dest_dir / "af_nextone.c").write_text(tone, encoding="utf-8")
    makefile = dest_dir / "Makefile"
    allfilters = dest_dir / "allfilters.c"
    make_text = patch_makefile(makefile.read_text(encoding="utf-8"))
    all_text = patch_allfilters(allfilters.read_text(encoding="utf-8"))
    makefile.write_text(make_text, encoding="utf-8")
    allfilters.write_text(all_text, encoding="utf-8")
