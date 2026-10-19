"""Host system name for the bars plate."""

import json
import threading
from urllib.request import Request, urlopen

import pytest

from nexencoder.console import Console, serve
from nexencoder.effects import (
    DEFAULT_SYSTEM_NAME,
    normalize_system_name,
    read_system_name,
    write_system_name,
)


def test_missing_name_is_the_default(tmp_path):
    assert read_system_name(tmp_path) == DEFAULT_SYSTEM_NAME
    assert DEFAULT_SYSTEM_NAME == "NexEncoder"


def test_normalize_collapses_and_limits():
    assert normalize_system_name("  Studio  A ") == "Studio A"
    assert normalize_system_name("News 12") == "News 12"
    assert normalize_system_name("name\nline") == "name line"
    for bad in ("", "   ", "this name is too long", "Studio_A", "A/B"):
        with pytest.raises(ValueError):
            normalize_system_name(bad)


def test_write_and_read_round_trip(tmp_path):
    assert write_system_name(tmp_path, "NexEncoder") == "NexEncoder"
    assert read_system_name(tmp_path) == "NexEncoder"
    assert (tmp_path / "name").read_text(encoding="utf-8") == "NexEncoder\n"
    (tmp_path / "name").write_text("not a legal_name\n", encoding="utf-8")
    assert read_system_name(tmp_path) == DEFAULT_SYSTEM_NAME


def test_system_name_feed(tmp_path):
    httpd = serve(Console(state_dir=tmp_path), "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/system-name", timeout=3) as response:
            listed = json.loads(response.read().decode("utf-8"))
        assert listed == {"ok": True, "name": "NexEncoder"}

        request = Request(
            f"http://127.0.0.1:{port}/api/system-name",
            data=json.dumps({"name": "Studio 1"}).encode("utf-8"),
            method="POST",
            headers={"X-Requested-With": "NexEncoder", "Content-Type": "application/json"},
        )
        with urlopen(request, timeout=3) as response:
            saved = json.loads(response.read().decode("utf-8"))
        assert saved == {"ok": True, "name": "Studio 1"}
        audit = (tmp_path / "audit.log").read_text(encoding="utf-8")
        assert "system.name" in audit and "Studio 1" in audit

        rejected = Request(
            f"http://127.0.0.1:{port}/api/system-name",
            data=json.dumps({"name": "way too many characters"}).encode("utf-8"),
            method="POST",
            headers={"X-Requested-With": "NexEncoder", "Content-Type": "application/json"},
        )
        from urllib.error import HTTPError
        with pytest.raises(HTTPError) as caught:
            urlopen(rejected, timeout=3)
        assert caught.value.code == 400
        assert read_system_name(tmp_path) == "Studio 1"
    finally:
        httpd.shutdown()
