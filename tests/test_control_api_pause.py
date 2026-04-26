"""Tests for the /api/daemon/{pause,resume,state} endpoints + the
synchronous _stats_cache update path.

We call the endpoint functions directly rather than via TestClient because
control_api's lifespan does heavy startup (Drive auth, MCP user seed,
multiple background pumps) that we don't want to spin up for a unit test.
The Depends(require_token) auth layer is exercised by the existing
require_token tests on other endpoints — its behavior here is identical
because the dependency is shared.
"""
from __future__ import annotations

import pytest

from src import control_api as capi
from src import db


def test_pause_endpoint_flips_state_and_pushes_cache(db_conn, restore_pause_state):
    # Start clean
    db.set_paused(db_conn, paused=False)
    capi._push_pause_to_cache({"paused": False, "since": None})

    state = capi.api_daemon_pause()

    assert state["paused"] is True
    assert state["since"] is not None

    # _stats_cache was updated synchronously
    with capi._stats_lock:
        assert capi._stats_cache["paused"] is True
        assert capi._stats_cache["paused_since"] == state["since"]

    # And in the DB
    paused, since = db.is_paused(db_conn)
    assert paused is True
    assert since is not None


def test_resume_endpoint_clears_state(db_conn, restore_pause_state):
    db.set_paused(db_conn, paused=True)
    capi._push_pause_to_cache({"paused": True, "since": "2026-04-26T00:00:00+00:00"})

    state = capi.api_daemon_resume()

    assert state["paused"] is False
    assert state["since"] is None
    with capi._stats_lock:
        assert capi._stats_cache["paused"] is False
        assert capi._stats_cache["paused_since"] is None


def test_pause_idempotent_preserves_since(db_conn, restore_pause_state):
    """The 'paused for X minutes' UI display only stays honest if back-to-
    back POSTs preserve the original transition timestamp."""
    db.set_paused(db_conn, paused=False)
    s1 = capi.api_daemon_pause()
    s2 = capi.api_daemon_pause()

    assert s1["paused"] is True
    assert s2["paused"] is True
    assert s1["since"] == s2["since"], "idempotent pause re-wrote `since`"


def test_state_endpoint_reads_db(db_conn, restore_pause_state):
    db.set_paused(db_conn, paused=True)
    state = capi.api_daemon_state()
    assert state["paused"] is True
    assert state["since"] is not None

    db.set_paused(db_conn, paused=False)
    state = capi.api_daemon_state()
    assert state["paused"] is False
    assert state["since"] is None


def test_try_log_event_swallows_errors():
    """A daemon_events.log_event() failure must NOT propagate — that would
    turn a successful pause into an HTTP 500."""

    class _ConnRaises:
        def cursor(self):
            raise RuntimeError("simulated DB outage")

    # Should not raise
    capi._try_log_event(_ConnRaises(), drive_id=None, level="info",
                        event="daemon_paused", message="test", extra=None)


def test_pause_then_state_consistency(db_conn, restore_pause_state):
    """End-to-end: pause -> state returns same since -> resume -> state shows null."""
    db.set_paused(db_conn, paused=False)

    paused_state = capi.api_daemon_pause()
    state = capi.api_daemon_state()
    assert state["paused"] is True
    assert state["since"] == paused_state["since"]

    capi.api_daemon_resume()
    state = capi.api_daemon_state()
    assert state["paused"] is False
    assert state["since"] is None
