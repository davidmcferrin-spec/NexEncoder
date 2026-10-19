"""Support zip windows, redaction, and the files that must stay out."""

import io
import zipfile
from datetime import datetime, timezone

import pytest

from nexencoder.support import DUMP_FILENAME, MAX_JOURNAL_BYTES, build_bundle, dump_and_clear, parse_hours, redact


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


def _quiet(argv):
    if argv[:2] == ["journalctl", "-u"] and "nexencoder-channel@A.service" in argv:
        return 0, "A" * (MAX_JOURNAL_BYTES + 32), ""
    return 0, "ok\n", ""


def test_bundle_still_caps_each_journal():
    _, data = build_bundle("all", runner=_quiet, hostname="box")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        log = archive.read("journal/nexencoder-channel@A.service.log")
    assert b"... truncated" in log
    assert len(log) < MAX_JOURNAL_BYTES + 64


def test_dump_stores_the_full_journal_before_it_clears(tmp_path):
    state = tmp_path / "state"
    seen: list[list[str]] = []

    def run(argv):
        seen.append(list(argv))
        if argv == ["journalctl", "--rotate"]:
            assert (state / DUMP_FILENAME).is_file()
            return 0, "", ""
        if argv == ["journalctl", "--vacuum-time=1s"]:
            return 0, "Vacuuming done\n", ""
        return _quiet(argv)

    name, data, detail = dump_and_clear(
        state_dir=state,
        runner=run,
        hostname="nex encoder",
        now=datetime(2026, 10, 18, 23, 0, tzinfo=timezone.utc),
    )
    assert detail == ""
    assert name == "nexencoder-support-20261018T230000Z-nex-encoder-all.zip"
    assert (state / DUMP_FILENAME).read_bytes() == data
    assert not (state / f".{DUMP_FILENAME}.part").exists()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        log = archive.read("journal/nexencoder-channel@A.service.log")
    assert b"... truncated" not in log
    assert len(log) > MAX_JOURNAL_BYTES
    rotate_at = seen.index(["journalctl", "--rotate"])
    vacuum_at = seen.index(["journalctl", "--vacuum-time=1s"])
    journal_at = max(i for i, argv in enumerate(seen) if argv[:2] == ["journalctl", "-u"])
    assert journal_at < rotate_at < vacuum_at


def test_dump_skips_vacuum_when_rotate_fails(tmp_path):
    seen: list[list[str]] = []

    def run(argv):
        seen.append(list(argv))
        if argv == ["journalctl", "--rotate"]:
            return 1, "", "rotate refused\nbecause busy"
        return 0, "ok\n", ""

    _, data, detail = dump_and_clear(state_dir=tmp_path / "state", runner=run, hostname="box")
    assert detail == "rotate refused because busy"
    assert data.startswith(b"PK")
    assert ["journalctl", "--vacuum-time=1s"] not in seen
    assert (tmp_path / "state" / DUMP_FILENAME).is_file()


def test_dump_returns_the_zip_when_vacuum_fails(tmp_path):
    def run(argv):
        if argv == ["journalctl", "--vacuum-time=1s"]:
            return 1, "", "vacuum refused"
        return 0, "ok\n", ""

    _, data, detail = dump_and_clear(state_dir=tmp_path / "state", runner=run, hostname="box")
    assert detail == "vacuum refused"
    assert data.startswith(b"PK")
    assert (tmp_path / "state" / DUMP_FILENAME).read_bytes() == data


def test_bundle_includes_the_audit_log_and_redacts_it(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "audit.log").write_text(
        '{"action":"config.save","settings":{"passphrase":"super-secret","url":"srt://caller:hunter2@example:9000"}}\n',
        encoding="utf-8",
    )
    _, data = build_bundle("24", runner=_quiet, hostname="box", state_dir=state)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        text = archive.read("audit/audit.log").decode("utf-8")
    assert "config.save" in text
    assert "super-secret" not in text
    assert "hunter2" not in text
    assert "REDACTED" in text


def test_dump_does_not_clear_when_the_copy_cannot_be_stored(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("x", encoding="utf-8")
    seen: list[list[str]] = []

    def run(argv):
        seen.append(list(argv))
        return 0, "ok\n", ""

    with pytest.raises(OSError):
        dump_and_clear(state_dir=blocked / "state", runner=run, hostname="box")
    assert ["journalctl", "--rotate"] not in seen
    assert ["journalctl", "--vacuum-time=1s"] not in seen
