"""Locate local Blackmagic archives and pull out the SDK headers and driver deb.

``setup.sh`` does not download Desktop Video. The operator puts
``Blackmagic_DeckLink_SDK_*.zip`` and ``Blackmagic_Desktop_Video_Linux_*.tar.gz``
in ``/home/nexstar``, or in the home directory of the user who started the script.
The highest version wins when more than one archive is present.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

SDK_HEADERS = ("DeckLinkAPI.h", "DeckLinkAPIDispatch.cpp", "DeckLinkAPIVersion.h")
OPERATOR_HOME = Path("/home/nexstar")


def search_dirs(home: str | None, sudo_home: str | None) -> list[Path]:
    """``/home/nexstar`` first, then the invoking home and the sudo user's home."""
    ordered = [OPERATOR_HOME]
    for raw in (home, sudo_home):
        if raw:
            ordered.append(Path(raw))
    unique: list[Path] = []
    for path in ordered:
        if path not in unique:
            unique.append(path)
    return unique


def _version_key(path: Path) -> tuple[int, ...]:
    match = re.search(r"(\d+(?:\.\d+)*)", path.name)
    if match is None:
        return (0,)
    return tuple(int(part) for part in match.group(1).split("."))


def latest_archive(directories: list[Path], pattern: str) -> Path | None:
    hits: list[Path] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        hits.extend(path for path in directory.glob(pattern) if path.is_file())
    if not hits:
        return None
    return max(hits, key=lambda path: (_version_key(path), path.name))


def latest_sdk(home: str | None, sudo_home: str | None) -> Path | None:
    return latest_archive(search_dirs(home, sudo_home), "Blackmagic_DeckLink_SDK_*.zip")


def latest_driver(home: str | None, sudo_home: str | None) -> Path | None:
    return latest_archive(search_dirs(home, sudo_home), "Blackmagic_Desktop_Video_Linux_*.tar.gz")


def extract_sdk(archive: Path, dest: Path) -> None:
    """Copy the Linux include directory out of a DeckLink SDK zip."""
    with zipfile.ZipFile(archive) as bundle:
        names = [name for name in bundle.namelist() if not name.endswith("/") and ".." not in Path(name).parts]
        grouped: dict[str, list[str]] = {}
        for name in names:
            folder, _, _base = name.rpartition("/")
            grouped.setdefault(folder, []).append(name)
        chosen = _linux_include(grouped)
        if chosen is None:
            raise ValueError(
                "DeckLink SDK zip has no Linux include with DeckLinkAPI.h, "
                "DeckLinkAPIDispatch.cpp, and DeckLinkAPIVersion.h"
            )
        dest.mkdir(parents=True, exist_ok=True)
        for name in grouped[chosen]:
            target = dest / Path(name).name
            target.write_bytes(bundle.read(name))
    missing = [name for name in SDK_HEADERS if not (dest / name).is_file()]
    if missing:
        raise ValueError("DeckLink SDK zip is missing " + ", ".join(missing))


def _linux_include(grouped: dict[str, list[str]]) -> str | None:
    required = set(SDK_HEADERS)

    def rank(folder: str) -> tuple[int, str]:
        text = folder.replace("\\", "/")
        linux = 0 if text.endswith("Linux/include") or "/Linux/include" in text else 1
        return (linux, text)

    for folder in sorted(grouped, key=rank):
        bases = {Path(name).name for name in grouped[folder]}
        if required <= bases and rank(folder)[0] == 0:
            return folder
    return None


def find_driver_deb(root: Path) -> Path:
    """The ``desktopvideo`` deb. The GUI package and RPM builds are ignored."""
    hits = [
        path
        for path in root.rglob("*.deb")
        if path.is_file() and path.name.startswith("desktopvideo_")
    ]
    if not hits:
        raise ValueError("Desktop Video archive has no desktopvideo deb")

    def rank(path: Path) -> tuple[int, tuple[int, ...], str]:
        text = path.as_posix()
        native = 0 if "/deb/x86_64/" in text or "/deb/amd64/" in text else 1
        return (native, tuple(-part for part in _version_key(path)), path.name)

    return min(hits, key=rank)
