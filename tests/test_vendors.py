"""Vendor records and the docker argv. No container is started."""

import pytest

from nexencoder.caption_feed import read_caption_source
from nexencoder.vendors import (
    VendorError,
    docker_argv,
    load_image,
    load_record,
    save_vendor,
    start_vendor,
    stop_vendor,
)


class _Done:
    def __init__(self, code=0, stdout="", stderr=""):
        self.returncode = code
        self.stdout = stdout
        self.stderr = stderr


def test_start_selects_vendor_captions_and_stop_returns_to_vanc(tmp_path):
    calls = []

    def run(argv):
        calls.append(list(argv))
        if argv[0] == "is-active":
            return _Done(stdout="inactive\n")
        return _Done()

    save_vendor(tmp_path, {"name": "vosk", "image": "example/cc:1", "channel": "A", "args": "--lang en"})
    started = start_vendor(tmp_path, "vosk", run=run)
    assert calls[0] == ["start", "nexencoder-vendor@vosk.service"]
    assert started["channel"] == "A"
    assert read_caption_source(tmp_path, "A") == "vendor"
    argv = docker_argv(load_record(tmp_path, "vosk"))
    assert argv[:4] == ["docker", "run", "--name", "nexencoder-vendor-vosk"]
    assert "--network" in argv and argv[argv.index("--network") + 1] == "host"
    image_at = argv.index("example/cc:1")
    assert argv[image_at + 1 :] == ["--lang", "en"]
    assert "NEXENCODER_CAPTION_PORT=19301" in argv
    assert "NEXENCODER_SPLICE_PORT=19101" in argv
    assert "NEXENCODER_FEED=239.255.1.1:19001" in argv
    stop_vendor(tmp_path, "vosk", run=run)
    assert read_caption_source(tmp_path, "A") == "vanc"


def test_a_failed_start_does_not_leave_vendor_captions_selected(tmp_path):
    def run(argv):
        if argv[0] == "start":
            return _Done(1, stderr="no such image")
        return _Done(stdout="inactive\n")

    save_vendor(tmp_path, {"name": "vosk", "image": "example/cc:1", "channel": "B"})
    with pytest.raises(VendorError):
        start_vendor(tmp_path, "vosk", run=run)
    assert read_caption_source(tmp_path, "B") == "vanc"


def test_stopping_one_sidecar_keeps_vendor_captions_while_another_is_up(tmp_path):
    def run(argv):
        if argv[0] == "is-active" and argv[1] == "nexencoder-vendor@other.service":
            return _Done(stdout="active\n")
        return _Done(stdout="inactive\n")

    save_vendor(tmp_path, {"name": "vosk", "image": "example/cc:1", "channel": "A"})
    save_vendor(tmp_path, {"name": "other", "image": "example/cc:1", "channel": "A"})
    start_vendor(tmp_path, "vosk", run=run)
    stop_vendor(tmp_path, "vosk", run=run)
    assert read_caption_source(tmp_path, "A") == "vendor"


def test_names_images_and_load_paths_are_rejected(tmp_path):
    with pytest.raises(VendorError):
        save_vendor(tmp_path, {"name": "Vosk", "image": "example/cc:1", "channel": "A"})
    with pytest.raises(VendorError):
        save_vendor(tmp_path, {"name": "vosk", "image": "example/cc:1;rm", "channel": "A"})
    with pytest.raises(VendorError):
        save_vendor(tmp_path, {"name": "vosk", "image": "example/cc:1", "channel": "C"})
    with pytest.raises(VendorError):
        load_image(tmp_path, "../secret.tar")
    with pytest.raises(VendorError):
        load_image(tmp_path, "missing.tar")
