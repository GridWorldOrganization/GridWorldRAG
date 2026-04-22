"""Integration tests that hit the real PostgreSQL + pgvector.

These require config/config.v2.env and a running PG. They use a test
drive_id / schema so they don't collide with production data.
"""
from __future__ import annotations

import time

import pytest

from src import db


def test_try_claim_drive_exclusive(db_conn):
    """A second conn cannot claim a drive that the first is holding."""
    conn2 = db.connect()
    try:
        drive_id = f"testclaim{int(time.time() * 1000)}"
        got1 = db.try_claim_drive(db_conn, drive_id)
        got2 = db.try_claim_drive(conn2, drive_id)
        assert got1 is True
        assert got2 is False
        # Release and retry with conn2
        db.release_drive(db_conn, drive_id)
        got2b = db.try_claim_drive(conn2, drive_id)
        assert got2b is True
        db.release_drive(conn2, drive_id)
    finally:
        conn2.close()


def test_try_claim_drive_released_on_conn_close():
    """Advisory lock auto-releases when the holder's connection closes,
    so a crashed worker never strands a drive."""
    drive_id = f"testclose{int(time.time() * 1000)}"
    conn_a = db.connect()
    assert db.try_claim_drive(conn_a, drive_id) is True
    conn_a.close()  # simulate crash
    conn_b = db.connect()
    try:
        assert db.try_claim_drive(conn_b, drive_id) is True
        db.release_drive(conn_b, drive_id)
    finally:
        conn_b.close()


def test_heartbeat_worker_upsert_and_partial_restoration(db_conn):
    """heartbeat_worker should UPSERT, and when a row is recreated after a
    zombie-cleanup DELETE, a subsequent hb should fill whatever fields it
    was given (explicit Nones stay None)."""
    wid = 99999
    try:
        db.heartbeat_worker(db_conn, wid, state="building",
                            drive_id="dA", drive_name="NameA",
                            current_file="f1.pdf",
                            files_done=3, total_files=10, phase="p1")
        rows = [w for w in db.list_workers(db_conn) if w["worker_id"] == wid]
        assert len(rows) == 1
        r = rows[0]
        assert r["state"] == "building"
        assert r["drive_name"] == "NameA"
        assert r["files_done"] == 3

        # Simulate zombie cleanup
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM public.daemon_workers WHERE worker_id=%s", (wid,))
        db_conn.commit()

        # Sparse hb — only files_done. Verify INSERT happens (default fields).
        db.heartbeat_worker(db_conn, wid, files_done=7)
        rows = [w for w in db.list_workers(db_conn) if w["worker_id"] == wid]
        assert len(rows) == 1
        r = rows[0]
        assert r["state"] == "idle"      # DEFAULT fires on re-INSERT
        assert r["drive_name"] is None   # not specified
        assert r["files_done"] == 7
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM public.daemon_workers WHERE worker_id=%s", (wid,))
        db_conn.commit()


def test_cleanup_zombies_deletes_old_rows(db_conn):
    wid = 99998
    try:
        db.heartbeat_worker(db_conn, wid, state="building",
                            drive_id="dZ", drive_name="Z",
                            current_file="x", files_done=1, total_files=2)
        # Force heartbeat_at into the past so it looks stale.
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE public.daemon_workers SET heartbeat_at = NOW() - INTERVAL '10 minutes'"
                " WHERE worker_id=%s",
                (wid,),
            )
        db_conn.commit()
        gc = db.cleanup_zombies(db_conn, stale_after_sec=60)
        assert wid in gc["workers_removed"]
        rows = [w for w in db.list_workers(db_conn) if w["worker_id"] == wid]
        assert rows == []
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM public.daemon_workers WHERE worker_id=%s", (wid,))
        db_conn.commit()


def test_upsert_file_chunks_atomic(db_conn, fresh_schema):
    """upsert_file_chunks should replace all existing chunks for the file
    atomically: success leaves new data only; failure leaves old data untouched."""
    drive_id, schema = fresh_schema

    def mkchunk(file_id, idx, content):
        return {
            "drive_file_id": f"{file_id}_chunk_{idx}",
            "title": "t", "content": content, "chunk_index": idx,
            "owner": "o", "source_url": "u", "file_type": "text/plain",
            "drive_modified_at": None,
            "embedding": [0.0] * 768,
        }

    result = db.upsert_file_chunks(db_conn, schema, "file1",
                                   [mkchunk("file1", 0, "a"), mkchunk("file1", 1, "b")])
    assert result == "added"

    # Re-upsert overwrites the same file_id's chunks
    result2 = db.upsert_file_chunks(db_conn, schema, "file1",
                                    [mkchunk("file1", 0, "new")])
    assert result2 == "updated"

    with db_conn.cursor() as cur:
        cur.execute(
            f'SELECT content FROM "{schema}".documents WHERE drive_file_id LIKE %s ESCAPE \'\\\'',
            (r"file1\_%",),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["content"] == "new"
