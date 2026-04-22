"""PostgreSQL + pgvector helpers (psycopg3).

Per-folder-drive architecture: each shared drive gets its own schema
named `fd_<drive_id>` containing a `documents` table. Public schema
holds `fd_registry` and `daemon_events`.

LIKE escape: drive_file_id contains underscores (base64url), so all LIKE
queries must escape `_` / `%` / `\\` and use ESCAPE '\\'.
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from typing import Iterable, Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from pgvector.psycopg import register_vector

from src.config import (
    PG_DATABASE, PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, EMBEDDING_DIM,
)

# ---------------------------------------------------------------------
# drive_id <-> schema name
# ---------------------------------------------------------------------
_DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def schema_for_drive(drive_id: str) -> str:
    """Derive a safe PostgreSQL identifier from a Google Drive ID.

    Google drive_ids are base64url (contains '-'), which is not valid in
    unquoted identifiers. We map '-' -> '_' so the schema name can appear
    unquoted inside composed identifiers like 'idx_<schema>_documents_*'.
    Collision risk is astronomically low in practice (drive_ids are 19+ chars).
    """
    if not _DRIVE_ID_RE.match(drive_id):
        raise ValueError(f"unsafe drive_id: {drive_id!r}")
    safe = drive_id.replace("-", "_")
    return f"fd_{safe}"


def _quote_ident(name: str) -> str:
    # psycopg 3 can use sql.Identifier, but we use it explicitly below.
    if not re.match(r"^[A-Za-z0-9_]+$", name):
        raise ValueError(f"invalid identifier: {name!r}")
    return f'"{name}"'


# ---------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------
def connect(dbname: Optional[str] = None, *, autocommit: bool = False) -> psycopg.Connection:
    conn = psycopg.connect(
        host=PG_HOST,
        port=PG_PORT,
        user=PG_USER,
        password=PG_PASSWORD,
        dbname=dbname or PG_DATABASE,
        autocommit=autocommit,
        row_factory=dict_row,
    )
    try:
        register_vector(conn)
    except Exception:
        # Vector type registration can fail if extension not yet loaded;
        # re-register after CREATE EXTENSION.
        pass
    return conn


@contextmanager
def cursor(conn: psycopg.Connection) -> Iterator[psycopg.Cursor]:
    cur = conn.cursor()
    try:
        yield cur
    finally:
        cur.close()


# ---------------------------------------------------------------------
# Database / schema bootstrap
# ---------------------------------------------------------------------
def ensure_database() -> None:
    """Create the target database if it does not exist. Uses postgres db."""
    admin = psycopg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD,
        dbname="postgres", autocommit=True,
    )
    try:
        with admin.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (PG_DATABASE,))
            if cur.fetchone() is None:
                cur.execute(f'CREATE DATABASE "{PG_DATABASE}"')
    finally:
        admin.close()


def apply_global_schema(conn: psycopg.Connection) -> None:
    """Apply schema.sql (public.fd_registry, public.daemon_events, vector ext).

    Re-registers the pgvector type on this connection after CREATE EXTENSION,
    so that callers who used connect() on an extension-less DB will now be able
    to insert VECTOR columns on the same connection.
    """
    from pathlib import Path
    sql_path = Path(__file__).resolve().parent.parent / "schema.sql"
    with open(sql_path, encoding="utf-8") as f:
        sql = f.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    try:
        register_vector(conn)
    except Exception:
        pass


# ---------------------------------------------------------------------
# Per-FD schema
# ---------------------------------------------------------------------
# Two placeholders here on purpose:
#   {schema_q}   -> quoted identifier for use as a schema reference ("fd_xxx")
#   {schema_raw} -> raw identifier for composition inside other identifier
#                   names (idx_<schema_raw>_documents_*) where quotes would
#                   be invalid SQL syntax.
_FD_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS {schema_q};

CREATE TABLE IF NOT EXISTS {schema_q}.documents (
    id                BIGSERIAL PRIMARY KEY,
    drive_file_id     TEXT UNIQUE NOT NULL,
    title             TEXT,
    content           TEXT,
    chunk_index       INTEGER,
    owner             TEXT,
    source_url        TEXT,
    file_type         TEXT,
    drive_modified_at TIMESTAMPTZ,
    embedding         VECTOR({dim}),
    sheet_gid         TEXT,
    sheet_name        TEXT,
    permissions       JSONB,
    partial_content   BOOLEAN NOT NULL DEFAULT FALSE,
    folder_path       TEXT DEFAULT '',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_{schema_raw}_documents_embedding
    ON {schema_q}.documents USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_{schema_raw}_documents_drive_file_id
    ON {schema_q}.documents (drive_file_id);
CREATE INDEX IF NOT EXISTS idx_{schema_raw}_documents_owner
    ON {schema_q}.documents (owner);
CREATE INDEX IF NOT EXISTS idx_{schema_raw}_documents_modified
    ON {schema_q}.documents (drive_modified_at);
CREATE INDEX IF NOT EXISTS idx_{schema_raw}_documents_sheet_gid
    ON {schema_q}.documents (sheet_gid);
"""


def ensure_fd_schema(conn: psycopg.Connection, drive_id: str) -> str:
    """Create the per-FD schema + tables + indexes if missing.

    PostgreSQL's `CREATE SCHEMA IF NOT EXISTS` is NOT race-free: concurrent
    callers can both see "does not exist", both proceed, and one loses with
    a pg_namespace unique-violation. We detect "already exists" (SQLSTATE
    42P06 `duplicate_schema` or 23505 `unique_violation` on pg_namespace)
    and treat it as success. Same for index name collisions (42P07).
    """
    schema = schema_for_drive(drive_id)
    sql = _FD_SCHEMA_SQL.format(
        schema_q=_quote_ident(schema),
        schema_raw=schema,
        dim=EMBEDDING_DIM,
    )
    for _attempt in range(3):
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
            return schema
        except psycopg.errors.DuplicateSchema:
            conn.rollback()
            return schema
        except psycopg.errors.DuplicateTable:
            conn.rollback()
            return schema
        except psycopg.errors.DuplicateObject:
            # e.g., index already exists from a parallel create
            conn.rollback()
            return schema
        except psycopg.errors.UniqueViolation as e:
            # Typically pg_namespace_nspname_index race. Re-check existence.
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM information_schema.schemata WHERE schema_name=%s",
                    (schema,),
                )
                if cur.fetchone() is not None:
                    return schema
            # Not there after rollback — unexpected, re-raise
            raise
        except Exception:
            conn.rollback()
            raise
    return schema


def drop_fd_schema(conn: psycopg.Connection, drive_id: str) -> None:
    schema = schema_for_drive(drive_id)
    with conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS {_quote_ident(schema)} CASCADE')
    conn.commit()


# ---------------------------------------------------------------------
# fd_registry
# ---------------------------------------------------------------------
def list_fds(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT drive_id, name, enabled, search_enabled, state,
                   last_sync_at, last_build_at,
                   file_count, chunk_count,
                   file_count_estimate, file_count_estimate_at,
                   rotate_token, pending_rotate_token, total_files_listed,
                   last_error, created_at, updated_at
            FROM public.fd_registry
            ORDER BY name
            """
        )
        return cur.fetchall()


def get_fd(conn: psycopg.Connection, drive_id: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM public.fd_registry WHERE drive_id=%s",
            (drive_id,),
        )
        return cur.fetchone()


def upsert_fd(conn: psycopg.Connection, drive_id: str, name: str,
              enabled: Optional[bool] = None) -> None:
    with conn.cursor() as cur:
        if enabled is None:
            cur.execute(
                """
                INSERT INTO public.fd_registry (drive_id, name)
                VALUES (%s, %s)
                ON CONFLICT (drive_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    updated_at = NOW()
                """,
                (drive_id, name),
            )
        else:
            cur.execute(
                """
                INSERT INTO public.fd_registry (drive_id, name, enabled)
                VALUES (%s, %s, %s)
                ON CONFLICT (drive_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    enabled = EXCLUDED.enabled,
                    updated_at = NOW()
                """,
                (drive_id, name, enabled),
            )
    conn.commit()


def set_enabled(conn: psycopg.Connection, drive_id: str, enabled: bool) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.fd_registry
               SET enabled=%s,
                   state = CASE WHEN %s THEN COALESCE(NULLIF(state,'disabled'),'idle') ELSE 'disabled' END,
                   updated_at = NOW()
             WHERE drive_id=%s
            """,
            (enabled, enabled, drive_id),
        )
    conn.commit()


def set_search_enabled(conn: psycopg.Connection, drive_id: str, enabled: bool) -> None:
    """Flip the MCP-search-scope flag for a drive. Independent of build enabled."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE public.fd_registry SET search_enabled=%s, updated_at=NOW() WHERE drive_id=%s",
            (enabled, drive_id),
        )
    conn.commit()


def set_state(conn: psycopg.Connection, drive_id: str, state: str,
              error: Optional[str] = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.fd_registry
               SET state=%s,
                   last_error = CASE WHEN %s='error' THEN %s ELSE NULL END,
                   updated_at = NOW()
             WHERE drive_id=%s
            """,
            (state, state, error, drive_id),
        )
    conn.commit()


def touch_sync(conn: psycopg.Connection, drive_id: str, *, built: bool = False) -> None:
    sql = """
        UPDATE public.fd_registry
           SET last_sync_at = NOW(),
               {maybe_build}
               updated_at = NOW()
         WHERE drive_id=%s
    """.format(maybe_build="last_build_at = NOW()," if built else "")
    with conn.cursor() as cur:
        cur.execute(sql, (drive_id,))
    conn.commit()


# ---------------------------------------------------------------------
# v0.4: build lifecycle helpers for the task-queue daemon
# ---------------------------------------------------------------------
def begin_build(conn: psycopg.Connection, drive_id: str,
                start_token: str, total_files: int) -> None:
    """Mark a build as in-progress with the start token captured (held in
    pending_rotate_token) and the total file count recorded. Idempotent."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.fd_registry
               SET state='building',
                   pending_rotate_token=%s,
                   total_files_listed=%s,
                   cancel_requested=FALSE,
                   last_error=NULL,
                   updated_at=NOW()
             WHERE drive_id=%s
            """,
            (start_token, total_files, drive_id),
        )
    conn.commit()


def commit_build(conn: psycopg.Connection, drive_id: str) -> None:
    """Finalize a build: commit pending_rotate_token → rotate_token,
    set state='idle', last_build_at=NOW, and recompute file/chunk counts
    via a single aggregate query on the FD schema."""
    schema = schema_for_drive(drive_id)
    with conn.cursor() as cur:
        # Recompute counts with our double split_part trick
        # (spreadsheets have {file_id}_sheet_{gid}_chunk_{n} — strip both).
        cur.execute(
            f"""
            SELECT COUNT(DISTINCT split_part(split_part(drive_file_id,'_chunk_',1),
                                              '_sheet_', 1)) AS files,
                   COUNT(*) AS chunks
              FROM {_quote_ident(schema)}.documents
            """
        )
        row = cur.fetchone() or {"files": 0, "chunks": 0}
        cur.execute(
            """
            UPDATE public.fd_registry
               SET rotate_token = COALESCE(pending_rotate_token, rotate_token),
                   pending_rotate_token = NULL,
                   total_files_listed = NULL,
                   cancel_requested = FALSE,
                   file_count = %s,
                   chunk_count = %s,
                   last_build_at = NOW(),
                   last_sync_at = NOW(),
                   state = 'idle',
                   updated_at = NOW()
             WHERE drive_id=%s
            """,
            (row["files"], row["chunks"], drive_id),
        )
    conn.commit()


def abort_build(conn: psycopg.Connection, drive_id: str, *,
                drop_schema: bool = False) -> None:
    """Clear build state when a cancelled drive is cleaned up. If
    drop_schema is True, the per-FD schema is also dropped (used when
    cancel happens during initial full-build where data was partial)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.fd_registry
               SET pending_rotate_token = NULL,
                   total_files_listed = NULL,
                   cancel_requested = FALSE,
                   state = 'idle',
                   updated_at = NOW()
             WHERE drive_id=%s
            """,
            (drive_id,),
        )
    conn.commit()
    if drop_schema:
        drop_fd_schema(conn, drive_id)


def request_cancel(conn: psycopg.Connection, drive_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE public.fd_registry SET cancel_requested=TRUE, updated_at=NOW() WHERE drive_id=%s",
            (drive_id,),
        )
    conn.commit()


def is_cancel_requested(conn: psycopg.Connection, drive_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cancel_requested FROM public.fd_registry WHERE drive_id=%s",
            (drive_id,),
        )
        r = cur.fetchone()
        return bool(r and r["cancel_requested"])


def inflight_workers_on_drive(conn: psycopg.Connection, drive_id: str) -> int:
    """Workers currently heart-beating on this drive in an active state.
    Used by the manager to decide whether finalize can be enqueued."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)::int AS n
              FROM public.daemon_workers
             WHERE drive_id = %s
               AND state IN ('claiming','listing','building','syncing')
               AND heartbeat_at > NOW() - INTERVAL '60 seconds'
            """,
            (drive_id,),
        )
        return int((cur.fetchone() or {"n": 0})["n"])


def set_file_count_estimate(conn: psycopg.Connection, drive_id: str, count: int) -> None:
    """Write the drive's cached file-count estimate. Caller does not need to
    manage transactions — commits on success, rolls back on error."""
    with conn.cursor() as cur:
        try:
            cur.execute(
                "UPDATE public.fd_registry "
                "   SET file_count_estimate=%s, file_count_estimate_at=NOW() "
                " WHERE drive_id=%s",
                (count, drive_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def set_rotate_token(conn: psycopg.Connection, drive_id: str, token: Optional[str]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE public.fd_registry SET rotate_token=%s, updated_at=NOW() WHERE drive_id=%s",
            (token, drive_id),
        )
    conn.commit()


def update_counts(conn: psycopg.Connection, drive_id: str) -> None:
    schema = schema_for_drive(drive_id)
    with conn.cursor() as cur:
        # drive_file_id layouts (see indexer.make_chunk_entry):
        #   regular:     {file_id}_chunk_{n}
        #   spreadsheet: {file_id}_sheet_{gid}_chunk_{n}
        # Strip the trailing _chunk_N first, then the optional _sheet_GID tail.
        cur.execute(
            f"""
            SELECT COUNT(DISTINCT split_part(split_part(drive_file_id, '_chunk_', 1),
                                              '_sheet_', 1)) AS files,
                   COUNT(*) AS chunks
            FROM {_quote_ident(schema)}.documents
            """
        )
        row = cur.fetchone() or {"files": 0, "chunks": 0}
        cur.execute(
            """
            UPDATE public.fd_registry
               SET file_count=%s, chunk_count=%s, updated_at=NOW()
             WHERE drive_id=%s
            """,
            (row["files"], row["chunks"], drive_id),
        )
    conn.commit()


def delete_fd(conn: psycopg.Connection, drive_id: str) -> None:
    drop_fd_schema(conn, drive_id)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM public.fd_registry WHERE drive_id=%s", (drive_id,))
    conn.commit()


# ---------------------------------------------------------------------
# Worker state (live multi-thread progress)
# ---------------------------------------------------------------------
def heartbeat_worker(conn: psycopg.Connection, worker_id: int, **fields) -> None:
    """UPSERT worker state. heartbeat_at is always set to NOW().

    Accepted fields: state, phase, drive_id, drive_name, current_file,
    files_done, total_files, started_at, last_error.
    """
    allowed = {
        "state", "phase", "drive_id", "drive_name", "current_file",
        "files_done", "total_files", "started_at", "last_error",
    }
    use = {k: v for k, v in fields.items() if k in allowed}
    cols = ["worker_id", "heartbeat_at"] + list(use.keys())
    vals: list = [worker_id]
    placeholders: list[str] = ["%s", "NOW()"]
    for k in use:
        placeholders.append("%s")
        vals.append(use[k])
    update_parts = [f"{k}=EXCLUDED.{k}" for k in use]
    update_parts.append("heartbeat_at=NOW()")
    sql = (
        f"INSERT INTO public.daemon_workers ({', '.join(cols)}) "
        f"VALUES ({', '.join(placeholders)}) "
        f"ON CONFLICT (worker_id) DO UPDATE SET "
        f"{', '.join(update_parts)}"
    )
    with conn.cursor() as cur:
        cur.execute(sql, vals)
    conn.commit()


def list_workers(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT worker_id, drive_id, drive_name, state, phase, current_file,
                   files_done, total_files, started_at, heartbeat_at, last_error
            FROM public.daemon_workers
            ORDER BY worker_id
            """
        )
        return cur.fetchall()


def clear_workers(conn: psycopg.Connection) -> None:
    """Truncate worker state — use at daemon startup."""
    with conn.cursor() as cur:
        cur.execute("TRUNCATE public.daemon_workers")
    conn.commit()


def cleanup_zombies(conn: psycopg.Connection, stale_after_sec: int = 90) -> dict:
    """Garbage-collect stale daemon state.

    1) daemon_workers rows with no heartbeat in `stale_after_sec` are deleted
       — they represent workers whose process/thread died without cleanup.
    2) fd_registry rows stuck in 'building' or 'syncing' with no live worker
       currently working on them are reset to 'idle', so the next sweep
       re-enqueues them and the work actually restarts.

    Returns: {"workers_removed": [...], "drives_reset": [...]}
    """
    removed_workers: list[int] = []
    reset_drives: list[str] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM public.daemon_workers
            WHERE heartbeat_at < NOW() - make_interval(secs => %s)
            RETURNING worker_id
            """,
            (stale_after_sec,),
        )
        removed_workers = [r["worker_id"] for r in cur.fetchall()]

        cur.execute(
            """
            UPDATE public.fd_registry f
               SET state='idle',
                   updated_at=NOW()
             WHERE state IN ('building','syncing')
               AND NOT EXISTS (
                 SELECT 1 FROM public.daemon_workers w
                  WHERE w.drive_id = f.drive_id
                    AND w.heartbeat_at > NOW() - INTERVAL '60 seconds'
                    AND w.state IN ('claiming','listing','building','syncing')
               )
            RETURNING drive_id
            """
        )
        reset_drives = [r["drive_id"] for r in cur.fetchall()]
    conn.commit()
    return {"workers_removed": removed_workers, "drives_reset": reset_drives}


def active_drive_ids(conn: psycopg.Connection) -> set[str]:
    """Return drive_ids currently being worked on (for de-dup in the enqueue step)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT drive_id FROM public.daemon_workers
            WHERE drive_id IS NOT NULL
              AND state IN ('claiming','listing','building','syncing')
              AND heartbeat_at > NOW() - INTERVAL '60 seconds'
            """
        )
        return {r["drive_id"] for r in cur.fetchall() if r.get("drive_id")}


# ---------------------------------------------------------------------
# MCP login users
# ---------------------------------------------------------------------
def list_mcp_users(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT username, created_at, updated_at FROM public.mcp_users ORDER BY username"
        )
        return cur.fetchall()


def upsert_mcp_user(conn: psycopg.Connection, username: str, password_hash: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.mcp_users (username, password_hash)
            VALUES (%s, %s)
            ON CONFLICT (username) DO UPDATE SET
                password_hash = EXCLUDED.password_hash,
                updated_at = NOW()
            """,
            (username, password_hash),
        )
    conn.commit()


def delete_mcp_user(conn: psycopg.Connection, username: str) -> int:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM public.mcp_users WHERE username=%s", (username,))
        n = cur.rowcount
    conn.commit()
    return n


def get_mcp_user_hash(conn: psycopg.Connection, username: str) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT password_hash FROM public.mcp_users WHERE username=%s",
            (username,),
        )
        r = cur.fetchone()
        return r["password_hash"] if r else None


# Per-user drive matrix removed in v0.3 — search_enabled is global. The
# public.mcp_user_drives table remains in the DB (idempotent IF NOT EXISTS)
# but no code reads it anymore; left for future reinstatement.


def seed_default_mcp_users(conn: psycopg.Connection) -> list[str]:
    """Create tobisako / izumi on first init (pw=admin) if they don't exist.
    Returns the list of usernames that were newly created.
    """
    from src.mcp_auth import hash_password
    created: list[str] = []
    defaults = [("tobisako", "admin"), ("izumi", "admin")]
    for username, pw in defaults:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM public.mcp_users WHERE username=%s", (username,))
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO public.mcp_users (username, password_hash) VALUES (%s, %s)",
                    (username, hash_password(pw)),
                )
                created.append(username)
    conn.commit()
    return created


# ---------------------------------------------------------------------
# Key-value config (daemon_config)
# ---------------------------------------------------------------------
def get_config(conn: psycopg.Connection, key: str,
               default: Optional[str] = None) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM public.daemon_config WHERE key=%s", (key,))
        r = cur.fetchone()
        return r["value"] if r else default


def set_config(conn: psycopg.Connection, key: str, value: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.daemon_config (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
            """,
            (key, value),
        )
    conn.commit()


# ---------------------------------------------------------------------
# Per-drive advisory lock (cross-process exclusion for build/sync)
# ---------------------------------------------------------------------
def try_claim_drive(conn: psycopg.Connection, drive_id: str) -> bool:
    """Acquire a pg_advisory_lock keyed on hashtext(drive_id).

    Returns True if the caller now holds the lock, False if another session
    holds it. Released by release_drive() or on conn close.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS got", (drive_id,))
        r = cur.fetchone()
        return bool(r and r.get("got"))


def release_drive(conn: psycopg.Connection, drive_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (drive_id,))


# ---------------------------------------------------------------------
# Events (daemon -> monitor)
# ---------------------------------------------------------------------
def log_event(conn: psycopg.Connection, *, drive_id: Optional[str], level: str,
              event: str, message: str = "", extra: Optional[dict] = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.daemon_events (drive_id, level, event, message, extra)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (drive_id, level, event, message,
             json.dumps(extra, ensure_ascii=False) if extra else None),
        )
    conn.commit()


def log_mcp_query(conn: psycopg.Connection, *, username: Optional[str],
                  tool_name: str, query: Optional[str],
                  returned_count: Optional[int], returned_ids: Optional[list],
                  latency_ms: int, error: Optional[str]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.mcp_query_log
                (username, tool_name, query, returned_count, returned_ids,
                 latency_ms, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (username, tool_name, query,
             returned_count,
             json.dumps(returned_ids, ensure_ascii=False) if returned_ids else None,
             latency_ms, error),
        )
    conn.commit()


def tail_mcp_query_log(conn: psycopg.Connection, limit: int = 50) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, ts, username, tool_name, query,
                   returned_count, returned_ids, latency_ms, error
            FROM public.mcp_query_log
            ORDER BY id DESC LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def tail_events(conn: psycopg.Connection, limit: int = 100,
                drive_id: Optional[str] = None) -> list[dict]:
    with conn.cursor() as cur:
        if drive_id:
            cur.execute(
                """
                SELECT id, ts, drive_id, level, event, message, extra
                FROM public.daemon_events
                WHERE drive_id=%s
                ORDER BY id DESC LIMIT %s
                """,
                (drive_id, limit),
            )
        else:
            cur.execute(
                """
                SELECT id, ts, drive_id, level, event, message, extra
                FROM public.daemon_events
                ORDER BY id DESC LIMIT %s
                """,
                (limit,),
            )
        return list(reversed(cur.fetchall()))


# ---------------------------------------------------------------------
# failed_files retry queue
# ---------------------------------------------------------------------
def add_failed_file(conn: psycopg.Connection, drive_id: str, file_id: str,
                    reason: str) -> None:
    entry = {"file_id": file_id, "reason": reason[:500]}
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.fd_registry
               SET failed_files = failed_files || %s::jsonb,
                   updated_at = NOW()
             WHERE drive_id=%s
            """,
            (json.dumps([entry]), drive_id),
        )
    conn.commit()


def get_failed_files(conn: psycopg.Connection, drive_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT failed_files FROM public.fd_registry WHERE drive_id=%s",
            (drive_id,),
        )
        row = cur.fetchone()
        if not row:
            return []
        return row["failed_files"] or []


def clear_failed_file(conn: psycopg.Connection, drive_id: str, file_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.fd_registry
               SET failed_files = COALESCE(
                     (SELECT jsonb_agg(f) FROM jsonb_array_elements(failed_files) f
                       WHERE f->>'file_id' <> %s),
                     '[]'::jsonb),
                   updated_at = NOW()
             WHERE drive_id=%s
            """,
            (file_id, drive_id),
        )
    conn.commit()


# ---------------------------------------------------------------------
# Chunk upsert / delete (per FD schema)
# ---------------------------------------------------------------------
def _escape_like_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _file_id_like_pattern(file_id: str) -> str:
    return _escape_like_literal(file_id) + r"\_%"


def insert_chunks(conn: psycopg.Connection, schema: str, chunks_data: Iterable[dict],
                  commit: bool = True) -> None:
    schema_q = _quote_ident(schema)
    sql = f"""
        INSERT INTO {schema_q}.documents
            (drive_file_id, title, content, chunk_index, owner,
             source_url, file_type, drive_modified_at, embedding,
             sheet_gid, sheet_name, permissions, partial_content, folder_path)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (drive_file_id) DO UPDATE SET
            title = EXCLUDED.title,
            content = EXCLUDED.content,
            chunk_index = EXCLUDED.chunk_index,
            owner = EXCLUDED.owner,
            source_url = EXCLUDED.source_url,
            file_type = EXCLUDED.file_type,
            drive_modified_at = EXCLUDED.drive_modified_at,
            embedding = EXCLUDED.embedding,
            sheet_gid = EXCLUDED.sheet_gid,
            sheet_name = EXCLUDED.sheet_name,
            permissions = EXCLUDED.permissions,
            partial_content = EXCLUDED.partial_content,
            folder_path = EXCLUDED.folder_path
    """
    with conn.cursor() as cur:
        try:
            for c in chunks_data:
                # PostgreSQL rejects NUL bytes
                title = (c.get("title") or "").replace("\x00", "")
                content = (c.get("content") or "").replace("\x00", "")
                perms = c.get("permissions")
                cur.execute(sql, (
                    c["drive_file_id"], title, content, c["chunk_index"],
                    c.get("owner"), c.get("source_url"), c.get("file_type"),
                    c.get("drive_modified_at"), c["embedding"],
                    c.get("sheet_gid"), c.get("sheet_name"),
                    json.dumps(perms, ensure_ascii=False) if perms else None,
                    bool(c.get("partial_content", False)),
                    c.get("folder_path", ""),
                ))
            if commit:
                conn.commit()
        except Exception:
            if commit:
                conn.rollback()
            raise


def delete_by_file_id(conn: psycopg.Connection, schema: str, file_id: str,
                      commit: bool = True) -> int:
    pattern = _file_id_like_pattern(file_id)
    with conn.cursor() as cur:
        try:
            cur.execute(
                f"DELETE FROM {_quote_ident(schema)}.documents "
                r"WHERE drive_file_id LIKE %s ESCAPE '\'",
                (pattern,),
            )
            deleted = cur.rowcount
            if commit:
                conn.commit()
            return deleted
        except Exception:
            if commit:
                conn.rollback()
            raise


def upsert_file_chunks(conn: psycopg.Connection, schema: str, file_id: str,
                       chunks: list[dict]) -> str:
    """Atomic delete + insert in a single transaction.

    Returns "updated" if chunks already existed, else "added".
    """
    pattern = _file_id_like_pattern(file_id)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT 1 FROM {_quote_ident(schema)}.documents "
            r"WHERE drive_file_id LIKE %s ESCAPE '\' LIMIT 1",
            (pattern,),
        )
        existed = cur.fetchone() is not None

    try:
        delete_by_file_id(conn, schema, file_id, commit=False)
        insert_chunks(conn, schema, chunks, commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return "updated" if existed else "added"


def file_exists(conn: psycopg.Connection, schema: str, file_id: str) -> bool:
    pattern = _file_id_like_pattern(file_id)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT 1 FROM {_quote_ident(schema)}.documents "
            r"WHERE drive_file_id LIKE %s ESCAPE '\' LIMIT 1",
            (pattern,),
        )
        return cur.fetchone() is not None


# ---------------------------------------------------------------------
# URL extraction (reused)
# ---------------------------------------------------------------------
def extract_file_id_from_url(url: str) -> Optional[str]:
    for pattern in (r"/d/([a-zA-Z0-9_-]+)",
                    r"/folders/([a-zA-Z0-9_-]+)",
                    r"[?&]id=([a-zA-Z0-9_-]+)"):
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def extract_gid_from_url(url: str) -> Optional[str]:
    m = re.search(r"[?&#]gid=(\d+)", url)
    return m.group(1) if m else None


# ---------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------
def search_enabled_schemas(conn: psycopg.Connection) -> list[tuple[str, str]]:
    """Return (drive_id, drive_name) pairs for drives with search_enabled=TRUE
    AND an existing fd_<drive_id> schema. Used by MCP search to know which
    schemas to query."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT drive_id, name
              FROM public.fd_registry
             WHERE search_enabled = TRUE
             ORDER BY name
            """
        )
        rows = cur.fetchall()
        result: list[tuple[str, str]] = []
        # Confirm the fd_<drive_id> schema actually exists (user may have
        # toggled search-on a drive whose build was later removed).
        for r in rows:
            drive_id = r["drive_id"]
            schema = schema_for_drive(drive_id)
            cur.execute(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name=%s",
                (schema,),
            )
            if cur.fetchone() is not None:
                result.append((drive_id, r["name"]))
        return result


def search_across_schemas(conn: psycopg.Connection, embedding,
                          schemas: list[tuple[str, str]],
                          n_results: int = 10,
                          owner: Optional[str] = None) -> list[dict]:
    """Run a cosine-distance search UNION-ALL across the given fd_* schemas,
    then take the top N. Each schema has its own documents table so we query
    each then merge."""
    if not schemas:
        return []
    import numpy as np
    if isinstance(embedding, list):
        embedding = np.array(embedding, dtype=np.float32)

    where = ""
    filter_params: list = []
    if owner:
        where = "WHERE owner = %s"
        filter_params.append(owner)

    # Build UNION ALL query referencing each schema.
    parts: list[str] = []
    for drive_id, _name in schemas:
        schema = schema_for_drive(drive_id)
        parts.append(
            f"""
            SELECT %s::text AS drive_id, title, content, owner, source_url,
                   file_type, drive_modified_at, sheet_gid, sheet_name,
                   folder_path, embedding <=> %s AS distance
              FROM {_quote_ident(schema)}.documents
              {where}
            """
        )

    sql = (
        "SELECT * FROM (\n"
        + "\nUNION ALL\n".join(parts)
        + "\n) sub ORDER BY distance ASC LIMIT %s"
    )

    params: list = []
    for drive_id, _name in schemas:
        params.append(drive_id)
        params.append(embedding)
        params.extend(filter_params)
    params.append(n_results)

    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        return list(cur.fetchall())


def global_stats(conn: psycopg.Connection) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)::int AS total_fds,
                   COUNT(*) FILTER (WHERE enabled)::int AS enabled_fds,
                   COALESCE(SUM(file_count),0)::int AS total_files,
                   COALESCE(SUM(chunk_count),0)::int AS total_chunks,
                   COALESCE(SUM(file_count_estimate),0)::int AS total_files_estimate,
                   COALESCE(SUM(file_count_estimate)
                              FILTER (WHERE enabled),0)::int AS enabled_files_estimate
            FROM public.fd_registry
            """
        )
        row = dict(cur.fetchone())
        # pg_database_size() walks the on-disk DB directory; under heavy
        # write load (many concurrent workers) this can take seconds.
        # Use a tight statement_timeout and fall back to -1 on slow.
        try:
            cur.execute("SET LOCAL statement_timeout = '1500ms'")
            cur.execute("SELECT pg_database_size(current_database())::bigint AS bytes")
            row["db_size_bytes"] = cur.fetchone()["bytes"]
        except Exception:
            conn.rollback()
            row["db_size_bytes"] = None
        return row
