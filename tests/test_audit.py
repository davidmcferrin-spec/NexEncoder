"""Operator audit lines and per-unit journal cutoffs."""

from datetime import datetime, timezone

from nexencoder.audit import (
    clear_journal_cutoffs,
    journal_since,
    mark_journal_cleared,
    read_audit,
    record,
)


def test_record_appends_and_redacts_secrets(tmp_path):
    moment = datetime(2026, 10, 19, 16, 4, 5, tzinfo=timezone.utc)
    record(tmp_path, "config.save", {
        "outputs": {"A": {"passphrase": "super-secret", "url": "srt://caller:hunter2@example:9000"}},
        "note": "keep",
    }, now=moment)
    record(tmp_path, "service.restart", {"unit": "nexencoder-channel@A.service", "ok": True}, now=moment)
    text = read_audit(tmp_path)
    lines = [line for line in text.splitlines() if line]
    assert len(lines) == 2
    assert "super-secret" not in text
    assert "hunter2" not in text
    assert "REDACTED" in text
    assert "keep" in text
    assert "2026-10-19T16:04:05Z" in text
    assert "service.restart" in lines[1]


def test_journal_cutoff_round_trip_ignores_a_bad_stamp(tmp_path):
    moment = datetime(2026, 10, 19, 16, 4, 5, tzinfo=timezone.utc)
    stamp = mark_journal_cleared(tmp_path, "prometheus.service", now=moment)
    assert stamp == "2026-10-19 16:04:05 UTC"
    assert journal_since(tmp_path, "prometheus.service") == stamp
    assert journal_since(tmp_path, "mediamtx.service") is None
    path = tmp_path / "journal-cutoffs.json"
    path.write_text('{"prometheus.service": "tomorrow", "mediamtx.service": "2026-10-19 16:04:05 UTC"}\n', encoding="utf-8")
    assert journal_since(tmp_path, "prometheus.service") is None
    assert journal_since(tmp_path, "mediamtx.service") == "2026-10-19 16:04:05 UTC"
    clear_journal_cutoffs(tmp_path)
    assert journal_since(tmp_path, "mediamtx.service") is None
