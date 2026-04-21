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
    if not _DRIVE_ID_RE.match(drive_id):
        raise ValueError(f"unsafe drive_id: {drive_id!r}")
    return f"fd_{drive_id}"


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
_FD_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.documents (
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

CREATE INDEX IF NOT EXISTS idx_{schema}_documents_embedding
    ON {schema}.documents USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_{schema}_documents_drive_file_id
    ON {schema}.documents (drive_file_id);
CREATE INDEX IF NOT EXISTS idx_{schema}_documents_owner
    ON {schema}.documents (owner);
CREATE INDEX IF NOT EXISTS idx_{schema}_documents_modified
    ON {schema}.documents (drive_modified_at);
CREATE INDEX IF NOT EXISTS idx_{schema}_documents_sheet_gid
    ON {schema}.documents (sheet_gid);
"""


def ensure_fd_schema(conn: psycopg.Connection, drive_id: str) -> str:
    schema = schema_for_drive(drive_id)
    sql = _FD_SCHEMA_SQL.format(schema=_quote_ident(schema), dim=EMBEDDING_DIM)
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
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
            SELECT drive_id, name, enabled, state,
                   last_sync_at, last_build_at,
                   file_count, chunk_count,
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
def global_stats(conn: psycopg.Connection) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)::int AS total_fds,
                   COUNT(*) FILTER (WHERE enabled)::int AS enabled_fds,
                   COALESCE(SUM(file_count),0)::int AS total_files,
                   COALESCE(SUM(chunk_count),0)::int AS total_chunks
            FROM public.fd_registry
            """
        )
        row = cur.fetchone()
        # DB size
        cur.execute("SELECT pg_database_size(current_database())::bigint AS bytes")
        row["db_size_bytes"] = cur.fetchone()["bytes"]
        return dict(row)
