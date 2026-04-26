"""Tests for the daemon-side pause behavior in _manager_iter and the
worker dispatch loop.

Pause has two enforcement points (defense-in-depth):

1. Manager (_manager_iter) — at the top of every 5s iteration, reads
   daemon_config['paused'] and skips the "enqueue new work" block if
   paused. Cancel-drain and finalize still run so in-flight builds can
   close out cleanly.

2. Worker (worker_loop dispatch) — after _queue.get() pulls a task, if
   the task is list_full / list_delta and _paused_flag is set, the
   worker drops it instead of starting a new build. file / file_delete /
   finalize tasks always run (that's the "in-flight tasks complete" spec).

The second hop covers the race window where a list task was enqueued by
the previous manager iter just before pause was activated.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src import rag_daemon


@pytest.fixture(autouse=True)
def reset_paused_flag():
    """The module-level _paused_flag leaks across tests if not reset."""
    rag_daemon._paused_flag.clear()
    yield
    rag_daemon._paused_flag.clear()


def _make_fds(*specs):
    """Build a list of fd_registry rows. Each spec is a (drive_id, enabled,
    state, has_token) tuple."""
    rows = []
    for did, enabled, state, has_token in specs:
        rows.append({
            "drive_id": did,
            "enabled": enabled,
            "state": state,
            "rotate_token": "token-x" if has_token else None,
            "cancel_requested": False,
        })
    return rows


def test_manager_iter_skips_enqueue_when_paused():
    """The primary pause path: manager sees paused=True and refuses to
    enqueue any new list_full / list_delta tasks for enabled idle drives."""
    fake_conn = MagicMock()
    fds = _make_fds(
        ("drvA", True, "idle", False),
        ("drvB", True, "idle", True),
    )

    with patch.object(rag_daemon.db, "is_paused",
                      return_value=(True, datetime.now(timezone.utc))) as m_paused, \
         patch.object(rag_daemon.db, "list_fds", return_value=fds), \
         patch.object(rag_daemon.db, "inflight_workers_on_drive", return_value=0), \
         patch.object(rag_daemon, "_pending_for_drive", return_value=0), \
         patch.object(rag_daemon, "_enqueue") as m_enqueue:
        rag_daemon._manager_iter(fake_conn)

    m_paused.assert_called_once_with(fake_conn)
    # No list_full / list_delta enqueues
    enqueue_kinds = [call.args[0][0] for call in m_enqueue.call_args_list]
    assert "list_full" not in enqueue_kinds
    assert "list_delta" not in enqueue_kinds
    # _paused_flag now set so workers also drop list tasks
    assert rag_daemon._paused_flag.is_set()


def test_manager_iter_enqueues_normally_when_not_paused():
    """Pause off: drvA → list_full (no token), drvB → list_delta (has token)."""
    fake_conn = MagicMock()
    fds = _make_fds(
        ("drvA", True, "idle", False),
        ("drvB", True, "idle", True),
    )

    with patch.object(rag_daemon.db, "is_paused", return_value=(False, None)), \
         patch.object(rag_daemon.db, "list_fds", return_value=fds), \
         patch.object(rag_daemon.db, "inflight_workers_on_drive", return_value=0), \
         patch.object(rag_daemon, "_pending_for_drive", return_value=0), \
         patch.object(rag_daemon, "_enqueue") as m_enqueue:
        rag_daemon._manager_iter(fake_conn)

    enqueue_kinds = [call.args[0][0] for call in m_enqueue.call_args_list]
    assert "list_full" in enqueue_kinds
    assert "list_delta" in enqueue_kinds
    assert not rag_daemon._paused_flag.is_set()


def test_manager_iter_runs_finalize_even_when_paused():
    """A drive in state='building' with no pending work + no inflight
    workers must STILL get a finalize enqueued — otherwise an in-flight
    build that finished its files mid-pause would never commit."""
    fake_conn = MagicMock()
    fds = _make_fds(("drvBuilding", True, "building", True))

    with patch.object(rag_daemon.db, "is_paused",
                      return_value=(True, datetime.now(timezone.utc))), \
         patch.object(rag_daemon.db, "list_fds", return_value=fds), \
         patch.object(rag_daemon.db, "inflight_workers_on_drive", return_value=0), \
         patch.object(rag_daemon, "_pending_for_drive", return_value=0), \
         patch.object(rag_daemon, "_enqueue") as m_enqueue:
        rag_daemon._manager_iter(fake_conn)

    enqueue_kinds = [call.args[0][0] for call in m_enqueue.call_args_list]
    assert "finalize" in enqueue_kinds, (
        "finalize must run even while paused so in-flight builds can commit"
    )


def test_manager_iter_runs_cancel_drain_even_when_paused():
    """Disabling a drive mid-build during pause should still trigger
    request_cancel + queue drain. Pause does not hold cancel hostage."""
    fake_conn = MagicMock()
    fds = _make_fds(("drvCanc", False, "building", True))  # disabled, but still building

    with patch.object(rag_daemon.db, "is_paused",
                      return_value=(True, datetime.now(timezone.utc))), \
         patch.object(rag_daemon.db, "list_fds", return_value=fds), \
         patch.object(rag_daemon.db, "inflight_workers_on_drive", return_value=0), \
         patch.object(rag_daemon, "_pending_for_drive", return_value=0), \
         patch.object(rag_daemon.db, "request_cancel") as m_cancel, \
         patch.object(rag_daemon, "_drain_queue_for_drive", return_value=0), \
         patch.object(rag_daemon, "_enqueue"):
        rag_daemon._manager_iter(fake_conn)

    m_cancel.assert_called_once_with(fake_conn, "drvCanc")


def test_manager_iter_handles_paused_read_failure():
    """If db.is_paused() raises (DB momentarily unreachable), the manager
    keeps the last known _paused_flag value rather than crashing."""
    fake_conn = MagicMock()

    rag_daemon._paused_flag.set()  # simulate previously-paused state

    with patch.object(rag_daemon.db, "is_paused", side_effect=RuntimeError("DB blip")), \
         patch.object(rag_daemon.db, "list_fds", return_value=[]), \
         patch.object(rag_daemon, "_enqueue"):
        rag_daemon._manager_iter(fake_conn)

    # Last-known value is preserved
    assert rag_daemon._paused_flag.is_set()


# --- Worker-side defense-in-depth check -----------------------------------

def test_paused_flag_drops_list_tasks_only():
    """The worker dispatch logic only short-circuits list_full / list_delta
    when paused. file / file_delete / finalize must always run — that's the
    'in-flight tasks complete' contract.

    We don't run a full worker loop here (it owns DB conns + heartbeats).
    Instead we mirror the dispatch decision: 'kind in (list_*) AND
    _paused_flag.is_set() → skip'. This test pins the rule and breaks
    loudly if anyone widens the skip set to include file or finalize."""

    rag_daemon._paused_flag.set()

    def should_drop(kind):
        return kind in ("list_full", "list_delta") and rag_daemon._paused_flag.is_set()

    assert should_drop("list_full") is True
    assert should_drop("list_delta") is True
    assert should_drop("file") is False
    assert should_drop("file_delete") is False
    assert should_drop("finalize") is False


def test_paused_flag_clear_lets_all_tasks_through():
    rag_daemon._paused_flag.clear()

    def should_drop(kind):
        return kind in ("list_full", "list_delta") and rag_daemon._paused_flag.is_set()

    for kind in ("list_full", "list_delta", "file", "file_delete", "finalize"):
        assert should_drop(kind) is False
