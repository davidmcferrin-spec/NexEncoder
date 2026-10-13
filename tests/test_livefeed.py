"""Waiting picture, signal log lines, and loop-file checks. No devices are opened."""

import pytest

from nexencoder.effects import read_flag, render_wait, write_flag, write_wait
from nexencoder.livefeed import check_loop_file, signal_from_decklink_log, srt_ingest_argv


def test_srt_ingest_keeps_pcm_off_the_progress_pipe():
    argv = srt_ingest_argv(9001, "1080i59.94", "/tmp/A.jpg")
    assert "pipe:7" in argv
    assert "pipe:3" not in argv


def test_decklink_log_lines_are_the_signal_flag():
    assert signal_from_decklink_log("No input signal detected") is False
    assert signal_from_decklink_log("[decklink] Input returned") is True
    assert signal_from_decklink_log("frame=12") is None


def test_wait_file_names_the_picture_and_the_ident(tmp_path):
    text = render_wait("slate", "10.0.0.8 INPUT 1", "")
    assert text.startswith("NXWAIT 1\n")
    assert "waiting slate\n" in text
    assert "ident 10.0.0.8 INPUT 1\n" in text
    assert "live -\n" in text
    write_wait(tmp_path, "A", "bars", "10.0.0.8 INPUT 2", "", "/var/lib/nexencoder/state/cg/A-live.nxcg")
    saved = (tmp_path / "A.wait").read_text(encoding="utf-8")
    assert "waiting bars\n" in saved
    assert "live /var/lib/nexencoder/state/cg/A-live.nxcg\n" in saved
    write_flag(tmp_path / "A.bars", True)
    assert read_flag(tmp_path / "A.bars", False) is True


def test_loop_file_must_exist_and_stay_between_ten_and_thirty_seconds(tmp_path):
    with pytest.raises(ValueError, match="needs a file"):
        check_loop_file("")
    missing = tmp_path / "missing.mp4"
    with pytest.raises(ValueError, match="not found"):
        check_loop_file(str(missing))
    clip = tmp_path / "loop.mp4"
    clip.write_bytes(b"not a movie")

    def runner(argv, **kwargs):
        class Result:
            stdout = b"Duration: 00:00:05.00, start: 0.000000, bitrate: 1 kb/s\n"
        return Result()

    with pytest.raises(ValueError, match="10 to 30"):
        check_loop_file(str(clip), runner=runner)
