"""US host time zones."""

import json
import subprocess
from datetime import datetime
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pytest

from nexencoder.console import Console, serve
from nexencoder.timezone import (
    TimezoneError,
    abbreviation,
    current_timezone,
    normalize_timezone,
    set_timezone,
)


def test_normalize_rejects_zones_outside_the_us_list():
    assert normalize_timezone("America/New_York") == "America/New_York"
    assert normalize_timezone("  America/Chicago ") == "America/Chicago"
    for bad in ("UTC", "Europe/London", "America/New_York;rm", "", "EST"):
        with pytest.raises(TimezoneError):
            normalize_timezone(bad)


def test_abbreviation_follows_daylight_time():
    winter = datetime(2026, 1, 15, 17, tzinfo=ZoneInfo("UTC"))
    summer = datetime(2026, 7, 15, 16, tzinfo=ZoneInfo("UTC"))
    assert abbreviation("America/New_York", winter) == "EST"
    assert abbreviation("America/New_York", summer) == "EDT"
    assert abbreviation("America/Phoenix", summer) == "MST"
    assert abbreviation("Pacific/Honolulu", summer) == "HST"


def test_current_timezone_reads_timedatectl():
    def run(args):
        assert args == ["show", "-p", "Timezone", "--value"]
        return subprocess.CompletedProcess(args, 0, stdout="America/Chicago\n", stderr="")

    body = current_timezone(run)
    assert body["timedatectl"] is True
    assert body["timezone"] == "America/Chicago"
    assert body["abbreviation"] in {"CST", "CDT"}
    assert body["zones"][0] == {"id": "America/New_York", "label": "Eastern (New York)"}


def test_set_timezone_allowlist_and_failure():
    calls = []

    def run(args):
        calls.append(args)
        if args[0] == "set-timezone":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="America/Los_Angeles\n", stderr="")

    body = set_timezone("America/Los_Angeles", run)
    assert calls[0] == ["set-timezone", "America/Los_Angeles"]
    assert body["timezone"] == "America/Los_Angeles"
    assert body["abbreviation"] in {"PST", "PDT"}

    def refuse(args):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="access denied")

    with pytest.raises(TimezoneError, match="access denied"):
        set_timezone("America/Denver", refuse)
    with pytest.raises(TimezoneError):
        set_timezone("Europe/Paris", run)


def test_timezone_feed_lists_and_saves(tmp_path):
    calls = []

    def run(args):
        calls.append(list(args))
        if args[0] == "show":
            zone = "America/Chicago" if any(item[0] == "set-timezone" for item in calls) else "UTC"
            return subprocess.CompletedProcess(args, 0, stdout=zone + "\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    import threading

    httpd = serve(Console(timezone_runner=run, state_dir=tmp_path), "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/timezone", timeout=3) as response:
            listed = json.loads(response.read().decode("utf-8"))
        assert listed["timezone"] == "UTC"
        assert any(zone["id"] == "America/New_York" for zone in listed["zones"])

        request = Request(
            f"http://127.0.0.1:{port}/api/timezone",
            data=json.dumps({"timezone": "America/Denver"}).encode("utf-8"),
            method="POST",
            headers={"X-Requested-With": "NexEncoder", "Content-Type": "application/json"},
        )
        with urlopen(request, timeout=3) as response:
            saved = json.loads(response.read().decode("utf-8"))
        assert saved["ok"] is True
        assert saved["timezone"] == "America/Denver"
        audit = (tmp_path / "audit.log").read_text(encoding="utf-8")
        assert "timezone.set" in audit
        assert "America/Denver" in audit
        assert ["set-timezone", "America/Denver"] in calls
    finally:
        httpd.shutdown()
