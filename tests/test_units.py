"""systemd units keep retrying a crashed service and do not give up."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNITS = ROOT / "systemd"


def _fields(name: str) -> dict[str, str]:
    fields = {}
    for line in (UNITS / name).read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or text.startswith("["):
            continue
        key, value = text.split("=", 1)
        fields[key] = value
    return fields


def test_services_retry_forever():
    for name in ("nexencoder-console.service", "mediamtx.service", "nexencoder-channel@.service"):
        fields = _fields(name)
        assert fields["Restart"] == "always"
        assert fields["StartLimitIntervalSec"] == "0"
        assert "BindsTo" not in fields


def test_channel_watchdog_and_config_exit():
    fields = _fields("nexencoder-channel@.service")
    assert fields["Type"] == "notify"
    assert fields["WatchdogSec"] == "90"
    assert fields["RestartPreventExitStatus"] == "78"
    assert "mediamtx.service" in fields["Wants"]
    assert "BindsTo" not in fields
