"""Shared pytest fixtures.

Tests run against the real PostgreSQL + pgvector instance configured in
config/config.v2.env (the same one the daemon/api use). Destructive DB
tests use dedicated schemas prefixed `test_fd_` which never collide with
production `fd_<drive_id>` schemas (drive_ids are base64url, never start
with "test").
"""
from __future__ import annotations

import os
# Allow importing src without requiring real config values to exist
os.environ.setdefault("WINSERVERRAG_SKIP_CONFIG", "0")

import sys
from pathlib import Path

# Make `src` importable regardless of cwd.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest


@pytest.fixture
def db_conn():
    """A live DB conn pointed at the project's PG. Commits roll forward —
    fixtures that need isolation should clean up after themselves."""
    from src import db
    conn = db.connect()
    yield conn
    try:
        conn.close()
    except Exception:
        pass


@pytest.fixture
def restore_pause_state(db_conn):
    """Snapshot daemon_config['paused'] before a test, restore after.

    Pause-state tests mutate the same row the live service reads, so they
    must not leak. snapshot=None means "key was absent before the test"
    and we DELETE it on teardown.
    """
    with db_conn.cursor() as cur:
        cur.execute("SELECT value FROM public.daemon_config WHERE key='paused'")
        r = cur.fetchone()
        snapshot = r["value"] if r else None
    yield
    with db_conn.cursor() as cur:
        if snapshot is None:
            cur.execute("DELETE FROM public.daemon_config WHERE key='paused'")
        else:
            cur.execute(
                "INSERT INTO public.daemon_config (key, value) VALUES ('paused', %s)"
                " ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
                (snapshot,),
            )
    db_conn.commit()


@pytest.fixture
def fresh_schema(db_conn):
    """Create a unique test schema, yield its name, drop it at teardown."""
    import uuid
    from src import db
    suffix = uuid.uuid4().hex[:12]
    drive_id = f"testdrv{suffix}"
    # bypass the _DRIVE_ID_RE check by going through public API
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.fd_registry (drive_id, name) VALUES (%s, %s)"
            " ON CONFLICT DO NOTHING",
            (drive_id, f"test drive {suffix}"),
        )
    db_conn.commit()
    schema = db.ensure_fd_schema(db_conn, drive_id)
    yield drive_id, schema
    with db_conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        cur.execute("DELETE FROM public.fd_registry WHERE drive_id=%s", (drive_id,))
        cur.execute("DELETE FROM public.mcp_user_drives WHERE drive_id=%s", (drive_id,))
    db_conn.commit()
