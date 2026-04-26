"""Tests for the Windows service registration probe used by /api/stats.

The probe shells out to `sc query <name>` for both WinServerRAG-API and
WinServerRAG-Daemon. We don't want unit tests to actually touch the
Service Control Manager (slow, host-dependent), so each test mocks
subprocess.run with a fake result that mimics one of the four real
states:

   1) registered + RUNNING       → sc rc=0, "STATE : RUNNING" in stdout
   2) registered + STOPPED       → sc rc=0, "STATE : STOPPED" in stdout
   3) not registered             → sc rc=1060
   4) sc.exe missing (Linux CI)  → FileNotFoundError

Returned label aggregation:
   both registered → "nssm"
   exactly one     → "partial"
   neither         → "manual"
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

from src import control_api


def _fake_sc_result(rc: int, stdout: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")


def test_both_running_returns_nssm():
    running = _fake_sc_result(0, "        STATE              : 4  RUNNING\n")
    with patch.object(subprocess, "run", return_value=running):
        out, label = control_api._check_service_registration()
    assert label == "nssm"
    assert out["WinServerRAG-API"]["registered"] is True
    assert out["WinServerRAG-API"]["running"] is True
    assert out["WinServerRAG-Daemon"]["registered"] is True
    assert out["WinServerRAG-Daemon"]["running"] is True


def test_both_registered_but_stopped_still_nssm():
    """Service was installed but is not currently running. The
    registration label still says nssm — running state is a separate
    field per service and surfaces through the per-service dict."""
    stopped = _fake_sc_result(0, "        STATE              : 1  STOPPED\n")
    with patch.object(subprocess, "run", return_value=stopped):
        out, label = control_api._check_service_registration()
    assert label == "nssm"
    assert out["WinServerRAG-API"]["registered"] is True
    assert out["WinServerRAG-API"]["running"] is False


def test_neither_registered_returns_manual():
    """Default dev mode: venv-launched, no NSSM registration. sc returns
    rc=1060 'service does not exist'."""
    not_found = _fake_sc_result(1060, "")
    with patch.object(subprocess, "run", return_value=not_found):
        out, label = control_api._check_service_registration()
    assert label == "manual"
    assert all(not v["registered"] for v in out.values())
    assert all(not v["running"] for v in out.values())


def test_exactly_one_registered_returns_partial():
    """Half-uninstall / manual NSSM tweak — flagged so the operator
    notices the asymmetry. Uses side_effect to return different fakes
    per call."""
    running = _fake_sc_result(0, "        STATE              : 4  RUNNING\n")
    not_found = _fake_sc_result(1060, "")
    with patch.object(subprocess, "run", side_effect=[running, not_found]):
        out, label = control_api._check_service_registration()
    assert label == "partial"
    # First service queried (alphabetical or list order — currently API):
    api = out["WinServerRAG-API"]
    daemon = out["WinServerRAG-Daemon"]
    assert (api["registered"] is True and daemon["registered"] is False) or \
           (api["registered"] is False and daemon["registered"] is True)


def test_sc_missing_does_not_raise():
    """On Linux CI runners, sc.exe is absent — FileNotFoundError must be
    swallowed and treated as 'not registered' so the API stays alive."""
    with patch.object(subprocess, "run", side_effect=FileNotFoundError("sc")):
        out, label = control_api._check_service_registration()
    assert label == "manual"
    assert all(not v["registered"] for v in out.values())


def test_sc_timeout_does_not_raise():
    """A hung Service Control Manager must not block the stats pump.
    subprocess.TimeoutExpired is treated as 'not registered'."""
    with patch.object(subprocess, "run",
                      side_effect=subprocess.TimeoutExpired(cmd="sc", timeout=2)):
        out, label = control_api._check_service_registration()
    assert label == "manual"
    assert all(not v["registered"] for v in out.values())


def test_running_field_only_true_when_state_running():
    """Don't mistake 'STOPPED' or 'PAUSED' for 'RUNNING' just because
    the service is registered. The running flag is the precise
    distinction the mini-monitor uses."""
    states = {
        "        STATE              : 4  RUNNING\n":     True,
        "        STATE              : 1  STOPPED\n":     False,
        "        STATE              : 7  PAUSED\n":      False,
        "        STATE              : 3  STOP_PENDING\n": False,
        "":                                                False,
    }
    for stdout, expected_running in states.items():
        with patch.object(subprocess, "run",
                          return_value=_fake_sc_result(0, stdout)):
            out, _ = control_api._check_service_registration()
        assert out["WinServerRAG-API"]["running"] is expected_running, (
            f"running flag wrong for STATE line: {stdout!r}")
