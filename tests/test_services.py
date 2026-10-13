"""Allowlisted service control."""

import subprocess

import pytest

from nexencoder.services import ServiceError, ServiceUnavailable, act, list_units, normalize_unit


def test_normalize_unit_allowlist():
    assert normalize_unit("prometheus") == "prometheus.service"
    assert normalize_unit("prometheus.service") == "prometheus.service"
    for bad in ("nginx", "prometheus.service;id", "../prometheus.service", "", "prometheus.timer"):
        with pytest.raises(ServiceError):
            normalize_unit(bad)


def test_list_units_parses_show():
    def run(args):
        assert args[0] == "show"
        stdout = "ActiveState=active\nSubState=running\nUnitFileState=enabled\nDescription=Prometheus\n"
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    body = list_units(run)
    assert body["systemctl"] is True
    assert len(body["units"]) == 6
    assert body["units"][1]["name"] == "prometheus.service"
    assert body["units"][2]["active"] == "active"
    assert body["units"][2]["enabled"] == "enabled"


def test_list_units_when_systemctl_is_missing():
    def run(args):
        raise ServiceUnavailable("systemctl is not available on this host")

    body = list_units(run)
    assert body["systemctl"] is False
    assert all(unit["active"] == "unavailable" for unit in body["units"])


def test_act_rejects_unknown_action_and_surfaces_stderr():
    with pytest.raises(ServiceError):
        act("prometheus.service", "daemon-reload", lambda args: None)

    def run(args):
        assert args == ["restart", "prometheus.service"]
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="access denied")

    with pytest.raises(ServiceError, match="access denied"):
        act("prometheus", "restart", run)
