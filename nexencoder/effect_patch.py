"""Install the nexeffect filter into an FFmpeg 9.0.2 source tree.

setup.sh runs this after unpacking the pinned tarball and before configure.
The filter source lives next to this module so the lerp and the composite
order stay in the repo, not only inside a downloaded tree.
"""

from __future__ import annotations

import re
from pathlib import Path

MARKER = "nexencoder effect stack"
# FFmpeg 9.0.2 pads the += column, so the gap is not a single space.
MAKE_ANCHOR = re.compile(r"^OBJS-\$\(CONFIG_COPY_FILTER\)[ \t]+\+=[ \t]+vf_copy\.o\n", re.MULTILINE)
MAKE_LINE = (
    "OBJS-$(CONFIG_NEXEFFECT_FILTER) += vf_nexeffect.o\n"
    "OBJS-$(CONFIG_NEXTONE_FILTER) += af_nextone.o\n"
    "OBJS-$(CONFIG_NEXCAPTION_FILTER) += vf_nexcaption.o\n"
)
CAPTION_MAKE = "OBJS-$(CONFIG_NEXCAPTION_FILTER) += vf_nexcaption.o\n"
ALL_ANCHOR = "extern const FFFilter ff_vf_perspective;\n"
ALL_LINE = (
    "extern const FFFilter ff_af_nextone;\n"
    "extern const FFFilter ff_vf_nexcaption;\n"
    "extern const FFFilter ff_vf_nexeffect;\n"
)
CAPTION_DECL = "extern const FFFilter ff_vf_nexcaption;\n"


def filter_source() -> str:
    return Path(__file__).with_name("vf_nexeffect.c").read_text(encoding="utf-8")


def tone_source() -> str:
    return Path(__file__).with_name("af_nextone.c").read_text(encoding="utf-8")


def caption_source() -> str:
    return Path(__file__).with_name("vf_nexcaption.c").read_text(encoding="utf-8")


def patch_makefile(text: str) -> str:
    if "CONFIG_NEXCAPTION_FILTER" in text and "CONFIG_NEXEFFECT_FILTER" in text:
        return text
    if "CONFIG_NEXEFFECT_FILTER" not in text:
        match = MAKE_ANCHOR.search(text)
        if match is None:
            raise ValueError("libavfilter Makefile has no copy filter anchor")
        return text[: match.end()] + MAKE_LINE + text[match.end() :]
    anchor = "OBJS-$(CONFIG_NEXTONE_FILTER) += af_nextone.o\n"
    if anchor not in text:
        raise ValueError("libavfilter Makefile has no nextone object to anchor the caption filter on")
    return text.replace(anchor, anchor + CAPTION_MAKE, 1)


def patch_allfilters(text: str) -> str:
    if "ff_vf_nexcaption" in text and "ff_vf_nexeffect" in text:
        return text
    if "ff_vf_nexeffect" not in text:
        if ALL_ANCHOR not in text:
            raise ValueError("allfilters.c has no perspective declaration to anchor on")
        return text.replace(ALL_ANCHOR, ALL_LINE + ALL_ANCHOR, 1)
    needle = "extern const FFFilter ff_vf_nexeffect;\n"
    return text.replace(needle, CAPTION_DECL + needle, 1)


def install_filter(src_root: str | Path) -> None:
    root = Path(src_root)
    source = filter_source()
    tone = tone_source()
    caption = caption_source()
    if MARKER not in source:
        raise ValueError("nexeffect source is missing its marker")
    if "nexencoder tone" not in tone:
        raise ValueError("nextone source is missing its marker")
    if "nexencoder caption slot" not in caption:
        raise ValueError("nexcaption source is missing its marker")
    dest_dir = root / "libavfilter"
    if not dest_dir.is_dir():
        raise ValueError("ffmpeg source has no libavfilter directory")
    (dest_dir / "vf_nexeffect.c").write_text(source, encoding="utf-8")
    (dest_dir / "af_nextone.c").write_text(tone, encoding="utf-8")
    (dest_dir / "vf_nexcaption.c").write_text(caption, encoding="utf-8")
    makefile = dest_dir / "Makefile"
    allfilters = dest_dir / "allfilters.c"
    make_text = patch_makefile(makefile.read_text(encoding="utf-8"))
    all_text = patch_allfilters(allfilters.read_text(encoding="utf-8"))
    makefile.write_text(make_text, encoding="utf-8")
    allfilters.write_text(all_text, encoding="utf-8")
