"""Tests for v0.4 build lifecycle helpers (begin/commit/abort/cancel).

Uses the existing fresh_schema fixture, which creates a throwaway
fd_<test-id> drive and cleans up after the test."""
from __future__ import annotations

from src import db


def test_begin_build_sets_pending_token_and_total(db_conn, fresh_schema):
    drive_id, _ = fresh_schema
    db.begin_build(db_conn, drive_id, start_token="tok-001", total_files=42)
    row = db.get_fd(db_conn, drive_id)
    assert row["state"] == "building"
    assert row["pending_rotate_token"] == "tok-001"
    assert row["total_files_listed"] == 42
    assert row["cancel_requested"] is False


def test_commit_build_advances_rotate_token_and_resets(db_conn, fresh_schema):
    drive_id, schema = fresh_schema
    db.begin_build(db_conn, drive_id, start_token="tok-new", total_files=0)
    db.commit_build(db_conn, drive_id)
    row = db.get_fd(db_conn, drive_id)
    assert row["rotate_token"] == "tok-new"
    assert row["pending_rotate_token"] is None
    assert row["total_files_listed"] is None
    assert row["state"] == "idle"
    assert row["last_build_at"] is not None


def test_commit_build_computes_file_count_via_aggregate(db_conn, fresh_schema):
    drive_id, schema = fresh_schema

    def mkchunk(fid, idx, content="x"):
        return {
            "drive_file_id": f"{fid}_chunk_{idx}",
            "title": "t", "content": content, "chunk_index": idx,
            "owner": "o", "source_url": "u", "file_type": "text/plain",
            "drive_modified_at": None,
            "embedding": [0.0] * 768,
        }

    db.upsert_file_chunks(db_conn, schema, "fileA",
                          [mkchunk("fileA", 0), mkchunk("fileA", 1)])
    db.upsert_file_chunks(db_conn, schema, "fileB", [mkchunk("fileB", 0)])
    db.begin_build(db_conn, drive_id, start_token="tok", total_files=2)
    db.commit_build(db_conn, drive_id)
    row = db.get_fd(db_conn, drive_id)
    assert row["file_count"] == 2
    assert row["chunk_count"] == 3


def test_request_cancel_sets_flag_and_is_queryable(db_conn, fresh_schema):
    drive_id, _ = fresh_schema
    assert db.is_cancel_requested(db_conn, drive_id) is False
    db.request_cancel(db_conn, drive_id)
    assert db.is_cancel_requested(db_conn, drive_id) is True


def test_abort_build_clears_pending_state(db_conn, fresh_schema):
    drive_id, _ = fresh_schema
    db.begin_build(db_conn, drive_id, start_token="tok", total_files=10)
    db.request_cancel(db_conn, drive_id)
    db.abort_build(db_conn, drive_id)
    row = db.get_fd(db_conn, drive_id)
    assert row["pending_rotate_token"] is None
    assert row["total_files_listed"] is None
    assert row["cancel_requested"] is False
    assert row["state"] == "idle"


def test_inflight_workers_on_drive_counts_only_active_states(db_conn, fresh_schema):
    drive_id, _ = fresh_schema
    # Seed a worker heartbeat on this drive in "building" state
    db.heartbeat_worker(db_conn, 88888, state="building", drive_id=drive_id,
                        drive_name="x", current_file=None,
                        files_done=0, total_files=0, phase="processing_file",
                        last_error=None)
    try:
        assert db.inflight_workers_on_drive(db_conn, drive_id) >= 1
        # Transition to idle — should drop out of inflight
        db.heartbeat_worker(db_conn, 88888, state="idle", drive_id=None,
                            drive_name=None, current_file=None,
                            files_done=0, total_files=0, phase=None,
                            last_error=None)
        assert db.inflight_workers_on_drive(db_conn, drive_id) == 0
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM public.daemon_workers WHERE worker_id=88888")
        db_conn.commit()
