"""Support zip windows, redaction, and the files that must stay out."""

import io
import zipfile
from datetime import datetime, timezone

import pytest

from nexencoder.support import build_bundle, parse_hours, redact


def test_parse_hours_accepts_the_operator_windows():
    assert parse_hours("24") == 24
    assert parse_hours("36") == 36
    assert parse_hours("ALL") is None
    with pytest.raises(ValueError):
        parse_hours("12")


def test_redact_hides_url_credentials_and_password_fields():
    raw = 'srt://caller:hunter2@example:9000 "passphrase": "super-secret"'
    cleaned = redact(raw)
    assert "hunter2" not in cleaned
    assert "super-secret" not in cleaned
    assert "REDACTED" in cleaned


def test_bundle_uses_the_window_and_leaves_the_token_out(tmp_path):
    config = tmp_path / "encoder.json"
    config.write_text('{"outputs":{"A":{"passphrase":"super-secret"}}}\n', encoding="utf-8")
    (tmp_path / "noc.token").write_text("do-not-pack\n", encoding="utf-8")
    seen: list[list[str]] = []

    def run(argv):
        seen.append(list(argv))
        if argv[0] == "journalctl":
            return 0, "line srt://user:pw@host\n", ""
        if argv[0] == "uname":
            return 0, "Linux box\n", ""
        if argv[0] == "lsmod":
            return 0, "blackmagic_io 16384 4\n", ""
        if argv[0] == "dmesg":
            return 0, "blackmagic warn\n", ""
        return 0, '{"devices":[]}\n', ""

    name, data = build_bundle(
        "36",
        config_path=config,
        runner=run,
        hostname="nex encoder",
        now=datetime(2026, 10, 18, 23, 0, tzinfo=timezone.utc),
    )
    assert name == "nexencoder-support-20261018T230000Z-nex-encoder-36h.zip"
    channel = ["journalctl", "-u", "nexencoder-channel@A.service", "-o", "short-iso", "--no-pager", "--since", "36 hours ago"]
    assert channel in seen
    assert ["lsmod"] in seen
    assert ["dmesg", "-T", "--level=err,warn"] in seen
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        assert "config/encoder.json" in names
        assert "journal/nexencoder-channel@A.service.log" in names
        assert "journal/nexencoder-noc.service.log" in names
        assert "journal/apache2.service.log" in names
        assert "host/lsmod.txt" in names
        assert "host/dmesg-warn-err.txt" in names
        assert "host/decklink-status.txt" in names
        assert "blackmagic_io" in archive.read("host/lsmod.txt").decode("utf-8")
        assert "blackmagic warn" in archive.read("host/dmesg-warn-err.txt").decode("utf-8")
        assert not any("noc.token" in item for item in names)
        packed = archive.read("config/encoder.json").decode("utf-8")
        log = archive.read("journal/nexencoder-channel@A.service.log").decode("utf-8")
    assert "super-secret" not in packed
    assert "pw@" not in log
    assert b"do-not-pack" not in data

    seen.clear()
    build_bundle("all", config_path=config, runner=run, hostname="box")
    journals = [argv for argv in seen if argv and argv[0] == "journalctl"]
    assert journals
    assert all("--since" not in argv for argv in journals)
