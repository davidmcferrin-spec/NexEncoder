"""Local Blackmagic archives. Nothing here installs a package."""

import zipfile
from pathlib import Path

import pytest

from nexencoder.decklink_install import (
    extract_sdk,
    find_driver_deb,
    latest_archive,
    latest_driver,
    latest_sdk,
    search_dirs,
)


def test_search_starts_at_the_operator_home_and_drops_duplicates():
    assert search_dirs("/home/nexstar", None) == [Path("/home/nexstar")]
    assert search_dirs("/root", "/home/nexstar") == [Path("/home/nexstar"), Path("/root")]


def test_latest_archive_prefers_the_higher_version(tmp_path: Path, monkeypatch):
    older = tmp_path / "Blackmagic_Desktop_Video_Linux_15.0.tar.gz"
    newer = tmp_path / "Blackmagic_Desktop_Video_Linux_16.0.tar.gz"
    older.write_bytes(b"old")
    newer.write_bytes(b"new")
    assert latest_archive([tmp_path], "Blackmagic_Desktop_Video_Linux_*.tar.gz") == newer
    monkeypatch.setattr("nexencoder.decklink_install.OPERATOR_HOME", tmp_path)
    sdk = tmp_path / "Blackmagic_DeckLink_SDK_15.0.zip"
    sdk.write_bytes(b"sdk")
    assert latest_sdk(None, None) == sdk
    assert latest_driver(None, None) == newer


def test_extract_sdk_uses_the_linux_include(tmp_path: Path):
    archive = tmp_path / "Blackmagic_DeckLink_SDK_15.0.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("Blackmagic DeckLink SDK 15.0/Win/include/DeckLinkAPI.h", b"win")
        bundle.writestr("Blackmagic DeckLink SDK 15.0/Linux/include/DeckLinkAPI.h", b"linux-h")
        bundle.writestr("Blackmagic DeckLink SDK 15.0/Linux/include/DeckLinkAPIDispatch.cpp", b"linux-d")
        bundle.writestr("Blackmagic DeckLink SDK 15.0/Linux/include/DeckLinkAPIVersion.h", b"linux-v")
        bundle.writestr("Blackmagic DeckLink SDK 15.0/Linux/include/DeckLinkAPITypes.h", b"linux-t")
    dest = tmp_path / "include"
    extract_sdk(archive, dest)
    assert (dest / "DeckLinkAPI.h").read_bytes() == b"linux-h"
    assert (dest / "DeckLinkAPITypes.h").read_bytes() == b"linux-t"
    assert not (dest / "Win").exists()


def test_extract_sdk_rejects_a_zip_without_linux_headers(tmp_path: Path):
    archive = tmp_path / "Blackmagic_DeckLink_SDK_15.0.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("readme.txt", b"no headers")
    with pytest.raises(ValueError, match="Linux include"):
        extract_sdk(archive, tmp_path / "include")


def test_driver_deb_is_the_native_desktopvideo_package(tmp_path: Path):
    gui = tmp_path / "deb" / "x86_64" / "desktopvideo-gui_15.0a14_amd64.deb"
    native = tmp_path / "deb" / "x86_64" / "desktopvideo_15.0a14_amd64.deb"
    foreign = tmp_path / "deb" / "i386" / "desktopvideo_15.0a14_i386.deb"
    for path in (gui, native, foreign):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"deb")
    assert find_driver_deb(tmp_path) == native


def test_setup_installs_local_archives_and_does_not_download_them():
    script = Path("setup.sh").read_text(encoding="utf-8")
    assert "/home/nexstar" in script
    assert "nexencoder.decklink_install" in script
    assert "blackmagicdesign.com" not in script
    assert "Desktop Video are checked" not in script
    assert "systemctl restart nexencoder-channel@A nexencoder-channel@B" in script
    assert "ufw allow OpenSSH" in script
    assert "ufw allow 80/tcp" in script
    assert "ProxyPass" not in script


def test_setup_downloads_pinned_tsduck():
    script = Path("setup.sh").read_text(encoding="utf-8")
    assert "https://github.com/tsduck/tsduck/releases/download/v${tsduck_ver}/${tsduck_deb}" in script
    assert 'tsduck_ver="3.45-4798"' in script
    assert "tsduck_${tsduck_ver}.ubuntu24_${tsduck_arch}.deb" in script
    assert "0023689f76e75b64e45253771208bcf8ca95b91fbfd0900da097c729f3c39fde" in script
    assert "332ae85ec2fce378ae9d2ae872606a38192aeb62da6edcf784ce6466b780295f" in script
    assert "SCTE-35 insertion needs TSDuck from https://tsduck.io/" not in script
    assert "TSDuck is checked for, not downloaded" not in script
