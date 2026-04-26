"""Tests for db.is_paused / db.set_paused.

The daemon's pause state lives in a single JSON row at
daemon_config['paused']:

    value = '{"paused": bool, "since": ISO8601 | null}'

Storing it as one row gives us race-free atomic updates (both fields
written by one UPSERT). The helpers also enforce transition-only `since`
semantics: a no-op call (already in target state) preserves the original
timestamp, only a true state change rewrites it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src import db


def _read_raw(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM public.daemon_config WHERE key='paused'")
        r = cur.fetchone()
        return r["value"] if r else None


def test_is_paused_returns_false_when_key_absent(db_conn, restore_pause_state):
    # Make sure the key is absent for this test
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM public.daemon_config WHERE key='paused'")
    db_conn.commit()

    paused, since = db.is_paused(db_conn)
    assert paused is False
    assert since is None


def test_set_paused_true_writes_row_with_iso_since(db_conn, restore_pause_state):
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM public.daemon_config WHERE key='paused'")
    db_conn.commit()

    state = db.set_paused(db_conn, paused=True)

    assert state["paused"] is True
    assert state["since"] is not None
    # ISO8601 with timezone
    parsed = datetime.fromisoformat(state["since"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None

    # Round-trips via is_paused()
    paused, since = db.is_paused(db_conn)
    assert paused is True
    assert since is not None
    assert since.tzinfo is not None

    # Stored shape: JSON
    raw = _read_raw(db_conn)
    obj = json.loads(raw)
    assert obj["paused"] is True
    assert obj["since"] == state["since"]


def test_set_paused_false_clears_since(db_conn, restore_pause_state):
    db.set_paused(db_conn, paused=True)
    state = db.set_paused(db_conn, paused=False)

    assert state["paused"] is False
    assert state["since"] is None

    paused, since = db.is_paused(db_conn)
    assert paused is False
    assert since is None


def test_set_paused_idempotent_preserves_since(db_conn, restore_pause_state):
    """Double-clicking the pause button must NOT bump `since`."""
    s1 = db.set_paused(db_conn, paused=True)
    first_since = s1["since"]

    s2 = db.set_paused(db_conn, paused=True)
    assert s2["paused"] is True
    assert s2["since"] == first_since, (
        "idempotent set_paused(True) re-wrote `since`, breaking 'paused for X minutes' UX"
    )

    # And again — still preserved
    s3 = db.set_paused(db_conn, paused=True)
    assert s3["since"] == first_since


def test_set_paused_idempotent_resume_keeps_since_null(db_conn, restore_pause_state):
    db.set_paused(db_conn, paused=False)
    s1 = db.set_paused(db_conn, paused=False)
    assert s1["paused"] is False
    assert s1["since"] is None


def test_persistence_across_reconnect(db_conn, restore_pause_state):
    """Pause state must survive a daemon restart — that's why it's in DB."""
    db.set_paused(db_conn, paused=True)

    # Simulate process restart: open a fresh connection
    conn2 = db.connect()
    try:
        paused, since = db.is_paused(conn2)
        assert paused is True
        assert since is not None
    finally:
        conn2.close()


def test_malformed_json_degrades_to_not_paused(db_conn, restore_pause_state):
    """Corrupted daemon_config['paused'] row must not crash the daemon —
    silently fall back to (False, None) so the operator can recover by
    issuing a clean pause/resume call."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.daemon_config (key, value) VALUES ('paused', %s)"
            " ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            ("not valid json {{{",),
        )
    db_conn.commit()

    paused, since = db.is_paused(db_conn)
    assert paused is False
    assert since is None


def test_iso_with_z_suffix_parses(db_conn, restore_pause_state):
    """Some serializers emit '...Z' instead of '+00:00'. Both must parse."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.daemon_config (key, value) VALUES ('paused', %s)"
            " ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            (json.dumps({"paused": True, "since": "2026-04-26T12:00:00Z"}),),
        )
    db_conn.commit()

    paused, since = db.is_paused(db_conn)
    assert paused is True
    assert since is not None
    assert since.tzinfo is not None
