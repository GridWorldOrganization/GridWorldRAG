"""Multi-threaded RAG daemon (v0.4 — task-queue architecture).

Architecture
============

    Manager (1 thread)                 Workers (N threads, uniform)
    ──────────────────                 ─────────────────────────────
    ├─ enumerate fd_registry    ╲       ├─ task = _queue.get()
    ├─ for enabled & idle drives ╲      └─ dispatch:
    │   enqueue ("list_full", d)  ─────▶    ("list_full", d)
    ├─ for enabled & idle w/token ╲            list_files_in_drive
    │   enqueue ("list_delta", d)  ─────▶   ("list_delta", d)
    ├─ for disabled & building     ─────▶      list_changes
    │   request_cancel + drain queue │         ├─ drain 前検出
    ├─ for building drives where    │          └─ for f in files:
    │   queue empty & no inflight:  │              enqueue ("file", d, f)
    │   enqueue ("finalize", d)     │       ("file", d, file_info)
    │                                │           process_one_file
    │                                │       ("file_delete", d, fid)
    │                                │           tombstone the chunks
    │                                │       ("finalize", d)
    │                                │           commit_build (agg counts,
    │                                │           pending_rotate_token→rotate_token)
    │                                │
    └─ also: zombie cleanup, events TTL, reconnect manager_conn

Cancel semantics
----------------
`fd_registry.cancel_requested` is the cancel flag. Manager sets it when a
drive's enabled flips OFF while in state='building'. Workers check it
before processing each item and short-circuit. In-flight embedding is
NOT interrupted (by design — interrupting PyTorch kernels is unreliable).

Recovery
--------
- Worker dies mid-list: advisory lock auto-releases; next sweep sees
  state='building' but queue empty + inflight=0 → finalize enqueued →
  commit completes what was already processed. rotate_token advances
  if possible. If pending_rotate_token was set, it survives; otherwise
  state goes back to idle with no token change.
- Worker dies mid-file: file is lost from this build. On next sweep the
  drive looks complete (queue empty) and finalize runs. file_count will
  reflect actual DB content (aggregate query). Missing file gets picked
  up by next delta sync when its modifiedTime pushes it through
  Changes API, or next full rebuild.
- Daemon restart: clear_workers wipes stale worker rows. Manager sees
  drives in state='building' with pending_rotate_token; treats queue as
  empty and enqueues finalize to close them out.
"""
from __future__ import annotations

import os
import queue
import signal
import sys
import threading
import time
import traceback
from typing import Optional, Tuple

from src import config, db, drive_client as dc
from src.embedding import embed_batch
from src.indexer import make_chunk_entry, chunk_text
from src.lockfile import Lock, LockAcquireError
from src.logging_setup import setup_logger

log = setup_logger("rag_daemon")
LOCK_PATH = config.LOCK_DIR / "rag_daemon.lock"
MAX_WORKERS = 10
MIN_WORKERS = 1

_stop_flag = False
_global_stop = threading.Event()

# One shared queue for all task types. Items are tuples, first element is
# the task kind: "list_full" | "list_delta" | "file" | "file_delete" | "finalize"
_queue: queue.Queue = queue.Queue()

# Per-drive pending-item counter. Used for completion detection without
# scanning the queue. Incremented on put, decremented on get (for file
# and file_delete tasks only — list/finalize are bookkeeping, not work).
_queue_counts: dict[str, int] = {}
_queue_counts_lock = threading.Lock()

_workers_lock = threading.Lock()
_workers: dict[int, dict] = {}  # wid -> {thread, stop_event, conn}
_next_worker_id = 1


def _signal_handler(signum, frame):
    global _stop_flag
    _stop_flag = True
    _global_stop.set()
    log.warning("signal %s received, draining...", signum)


# ---------------------------------------------------------------------
# DB liveness helpers (Fix A from v0.3)
# ---------------------------------------------------------------------
def _conn_alive(conn) -> bool:
    if conn is None:
        return False
    try:
        if getattr(conn, "closed", False):
            return False
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return True
    except Exception:
        return False


def _replace_conn(conn, *, label: str = ""):
    if conn is not None:
        try: conn.rollback()
        except Exception: pass
        try: conn.close()
        except Exception: pass
    try:
        return db.connect()
    except Exception as e:
        log.error("%s reopen DB conn failed: %s", label or "conn", e)
        return None


def _min_free_bytes_ok() -> bool:
    import shutil
    try:
        drive = os.path.splitdrive(str(config.PROJECT_ROOT))[0] + "\\"
        usage = shutil.disk_usage(drive)
        return usage.free >= config.DAEMON_MIN_FREE_BYTES
    except Exception:
        return False


# ---------------------------------------------------------------------
# Queue helpers (counted)
# ---------------------------------------------------------------------
def _enqueue(task: tuple) -> None:
    """Push a task; update per-drive pending counter for file-type tasks."""
    kind = task[0]
    if kind in ("file", "file_delete"):
        drive_id = task[1]
        with _queue_counts_lock:
            _queue_counts[drive_id] = _queue_counts.get(drive_id, 0) + 1
    _queue.put(task)


def _mark_consumed(task: tuple) -> None:
    """Call after a file/file_delete task has been pulled from the queue
    and the worker is about to process it (or skip it). Decrements count."""
    kind = task[0]
    if kind in ("file", "file_delete"):
        drive_id = task[1]
        with _queue_counts_lock:
            n = _queue_counts.get(drive_id, 0) - 1
            _queue_counts[drive_id] = max(0, n)


def _pending_for_drive(drive_id: str) -> int:
    with _queue_counts_lock:
        return _queue_counts.get(drive_id, 0)


# ---------------------------------------------------------------------
# Worker context (heartbeat)
# ---------------------------------------------------------------------
_worker_ctx: dict[int, dict] = {}
_worker_ctx_lock = threading.Lock()


def _hb(conn, worker_id: Optional[int], **fields):
    if worker_id is None:
        return
    with _worker_ctx_lock:
        ctx = _worker_ctx.setdefault(worker_id, {})
        ctx.update(fields)
        full = dict(ctx)
    try:
        db.heartbeat_worker(conn, worker_id, **full)
    except Exception:
        try: conn.rollback()
        except Exception: pass


def _hb_reset(worker_id: int) -> None:
    with _worker_ctx_lock:
        _worker_ctx.pop(worker_id, None)


# ---------------------------------------------------------------------
# Task handlers
# ---------------------------------------------------------------------
def _handle_list(conn, wid: int, drive_id: str, mode: str) -> None:
    """mode = 'full' or 'delta'.

    Captures rotate_token BEFORE enumerating, then calls the appropriate
    Drive API (list_files_in_drive for full, list_changes for delta) and
    enqueues one file/file_delete task per item.

    Uses a drive-level advisory lock so two workers can never list the
    same drive concurrently; auto-released on conn close on worker crash.
    """
    row = db.get_fd(conn, drive_id)
    if not row:
        return
    drive_name = (row.get("name") or "")

    if db.is_cancel_requested(conn, drive_id):
        log.info("list: drive %s cancel_requested, skipping", drive_id)
        db.abort_build(conn, drive_id)
        return

    if not db.try_claim_drive(conn, drive_id):
        log.info("list: drive %s already being listed by another worker", drive_id)
        return

    try:
        _hb_reset(wid)
        _hb(conn, wid, state="listing", drive_id=drive_id, drive_name=drive_name,
            phase=f"list_{mode}_start", current_file=None, files_done=0,
            total_files=0, last_error=None)

        service = dc.get_drive_service()

        try:
            if mode == "full":
                start_token = dc.get_changes_start_token(service, drive_id)
            else:
                start_token = row.get("rotate_token")
                if not start_token:
                    log.warning("delta list for %s but no rotate_token; falling back to full",
                                drive_id)
                    start_token = dc.get_changes_start_token(service, drive_id)
                    mode = "full"
        except Exception as e:
            log.error("list: getStartPageToken failed for %s: %s", drive_id, e)
            db.set_state(conn, drive_id, "error", str(e))
            return

        _hb(conn, wid, phase=f"list_{mode}_enumerating")

        try:
            if mode == "full":
                files = dc.list_files_in_drive(service, drive_id)
                files = dc.attach_folder_paths(files, drive_name=drive_name)
                changes = [(False, f) for f in files]  # (is_delete, file_info)
                new_token = start_token
            else:
                raw_changes, new_token = dc.list_changes(service, start_token, drive_id)
                changes = []
                for ch in raw_changes:
                    fid = ch.get("fileId")
                    f = ch.get("file")
                    is_delete = bool(ch.get("removed") or (f and f.get("trashed")))
                    if is_delete:
                        changes.append((True, {"id": fid}))
                    elif f:
                        f["folder_path"] = f.get("folder_path", drive_name)
                        changes.append((False, f))
        except Exception as e:
            log.error("list: API enumerate failed for %s: %s", drive_id, e)
            db.set_state(conn, drive_id, "error", str(e))
            return

        total = len(changes)
        db.begin_build(conn, drive_id, start_token=new_token, total_files=total)
        # Pre-create the FD schema before unleashing N workers on it — avoids
        # the CREATE SCHEMA IF NOT EXISTS race on pg_namespace.
        db.ensure_fd_schema(conn, drive_id)
        log.info("[%s] (w#%s) list_%s — %d items", drive_id, wid, mode, total)

        enqueued = 0
        for is_delete, info in changes:
            if db.is_cancel_requested(conn, drive_id):
                log.info("list: cancel detected mid-enqueue at %d/%d", enqueued, total)
                break
            if is_delete:
                _enqueue(("file_delete", drive_id, info.get("id")))
            else:
                _enqueue(("file", drive_id, info))
            enqueued += 1

        _hb(conn, wid, total_files=enqueued, phase=f"list_{mode}_done")
    finally:
        try:
            db.release_drive(conn, drive_id)
        except Exception:
            try: conn.rollback()
            except Exception: pass


def _embed_and_chunks_for_file(file_info: dict, service) -> list[dict]:
    """Extract text, split into chunks, embed. Same as v0.3."""
    mime = file_info.get("mimeType", "")
    out: list[dict] = []

    if mime == "application/vnd.google-apps.spreadsheet":
        sheets = dc.extract_spreadsheet_sheets(file_info["id"])
        for sh in sheets:
            content = sh.get("content")
            failed = sh.get("failed", False)
            gid, name = sh["gid"], sh["name"]
            if not content:
                emb = embed_batch([f"{file_info.get('name','')} :: {name}"])[0]
                out.append(make_chunk_entry(
                    file_info, f"[シート] {file_info.get('name','')} / {name}",
                    emb, 0, sheet_gid=gid, sheet_name=name,
                    partial_content=bool(failed)))
                continue
            pieces = list(chunk_text(content, config.CHUNK_SIZE, config.CHUNK_OVERLAP))
            if not pieces:
                continue
            embs = embed_batch(pieces)
            for idx, (p, e) in enumerate(zip(pieces, embs)):
                out.append(make_chunk_entry(
                    file_info, p, e, idx, sheet_gid=gid, sheet_name=name))
        return out

    text, partial = dc.extract_text(service, file_info)
    if text is None:
        return []
    pieces = list(chunk_text(text, config.CHUNK_SIZE, config.CHUNK_OVERLAP))
    if not pieces:
        pieces = [f"[{mime}] {file_info.get('name','')}"]
        partial = True
    embs = embed_batch(pieces)
    for idx, (p, e) in enumerate(zip(pieces, embs)):
        out.append(make_chunk_entry(
            file_info, p, e, idx,
            partial_content=partial and idx == len(pieces) - 1))
    return out


def _handle_file(conn, wid: int, drive_id: str, file_info: dict) -> None:
    """Process one file: check cancel, embed, upsert chunks.

    Cancel check: happens BEFORE any Drive/embedding work. If set, the
    task is dropped silently. The in-flight embedding of the PREVIOUS
    file (if any) has already completed — we honor it but take no new
    work for this drive."""
    if db.is_cancel_requested(conn, drive_id):
        return

    row = db.get_fd(conn, drive_id)
    drive_name = (row or {}).get("name", "") or ""
    drive_total = int((row or {}).get("total_files_listed") or 0)
    # total_files tracks the drive-wide enumerated count so every worker (not
    # just the one that ran list_full) shows a populated progress bar.
    _hb(conn, wid, state="building", drive_id=drive_id, drive_name=drive_name,
        phase="processing_file", current_file=file_info.get("name", ""),
        total_files=drive_total)

    if not _min_free_bytes_ok():
        log.error("[%s] (w#%s) disk low, marking error", drive_id, wid)
        try: db.set_state(conn, drive_id, "error", "disk low")
        except Exception: pass
        return

    try:
        service = dc.get_drive_service()
        schema = db.ensure_fd_schema(conn, drive_id)
        chunks = _embed_and_chunks_for_file(file_info, service)
        if chunks:
            db.upsert_file_chunks(conn, schema, file_info["id"], chunks)
        # Count only this worker's successful files — shared progress would
        # need a per-drive counter, out of scope for a point fix.
        with _worker_ctx_lock:
            prev_done = int(_worker_ctx.get(wid, {}).get("files_done", 0) or 0)
        _hb(conn, wid, files_done=prev_done + 1)
    except Exception as e:
        log.warning("[%s] (w#%s) file %s failed: %s",
                    drive_id, wid, file_info.get("id"), e)
        try: conn.rollback()
        except Exception: pass
        try:
            db.add_failed_file(conn, drive_id, file_info["id"], str(e))
        except Exception:
            try: conn.rollback()
            except Exception: pass


def _handle_file_delete(conn, wid: int, drive_id: str, file_id: str) -> None:
    if db.is_cancel_requested(conn, drive_id):
        return
    row = db.get_fd(conn, drive_id)
    drive_name = (row or {}).get("name", "") or ""
    _hb(conn, wid, state="syncing", drive_id=drive_id, drive_name=drive_name,
        phase="deleting_file", current_file=file_id)
    try:
        schema = db.ensure_fd_schema(conn, drive_id)
        db.delete_by_file_id(conn, schema, file_id)
    except Exception as e:
        log.warning("[%s] (w#%s) delete %s failed: %s", drive_id, wid, file_id, e)
        try: conn.rollback()
        except Exception: pass


def _handle_finalize(conn, wid: int, drive_id: str) -> None:
    """Commit the build: aggregate counts + advance rotate_token.
    Idempotent; if cancel was requested, falls back to abort."""
    row = db.get_fd(conn, drive_id)
    if not row:
        return
    drive_name = (row.get("name") or "")
    _hb(conn, wid, state="building", phase="finalizing",
        drive_id=drive_id, drive_name=drive_name,
        current_file=None)

    try:
        if row.get("cancel_requested"):
            log.info("[%s] (w#%s) finalize: cancel_requested set → abort", drive_id, wid)
            db.abort_build(conn, drive_id)
            return
        db.commit_build(conn, drive_id)
        log.info("[%s] (w#%s) finalize: committed", drive_id, wid)
    except Exception as e:
        log.error("[%s] (w#%s) finalize failed: %s\n%s",
                  drive_id, wid, e, traceback.format_exc())
        try: conn.rollback()
        except Exception: pass


# ---------------------------------------------------------------------
# Worker thread main loop
# ---------------------------------------------------------------------
def _worker_loop(wid: int, personal_stop: threading.Event) -> None:
    conn = None
    try:
        conn = db.connect()
        db.heartbeat_worker(conn, wid, state="idle",
                            drive_id=None, drive_name=None, current_file=None,
                            files_done=0, total_files=0, phase=None, last_error=None)
        # Prime per-thread Drive service (thread-local).
        _ = dc.get_drive_service()
        log.info("worker %d started", wid)

        while not _global_stop.is_set() and not personal_stop.is_set():
            if not _conn_alive(conn):
                log.warning("worker %d: conn dead, exiting (manager will respawn)", wid)
                return

            try:
                task = _queue.get(timeout=2.0)
            except queue.Empty:
                _hb_reset(wid)
                try:
                    db.heartbeat_worker(conn, wid, state="idle",
                                        drive_id=None, drive_name=None, current_file=None,
                                        files_done=0, total_files=0, phase=None,
                                        last_error=None)
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
                    if not _conn_alive(conn):
                        return
                continue

            # Decrement pending counter for file tasks (even if skipped later)
            _mark_consumed(task)

            kind = task[0]
            try:
                if kind == "list_full":
                    _handle_list(conn, wid, task[1], mode="full")
                elif kind == "list_delta":
                    _handle_list(conn, wid, task[1], mode="delta")
                elif kind == "file":
                    _handle_file(conn, wid, task[1], task[2])
                elif kind == "file_delete":
                    _handle_file_delete(conn, wid, task[1], task[2])
                elif kind == "finalize":
                    _handle_finalize(conn, wid, task[1])
                else:
                    log.warning("unknown task kind: %s", kind)
            except Exception as e:
                log.error("worker %d task %s failed: %s\n%s",
                          wid, kind, e, traceback.format_exc())
                try: conn.rollback()
                except Exception: pass
                if not _conn_alive(conn):
                    return
    finally:
        if conn is not None:
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM public.daemon_workers WHERE worker_id=%s", (wid,))
                conn.commit()
            except Exception:
                pass
            try: conn.close()
            except Exception: pass
        with _workers_lock:
            _workers.pop(wid, None)
        log.info("worker %d exited", wid)


# ---------------------------------------------------------------------
# Worker pool management
# ---------------------------------------------------------------------
def _spawn_worker() -> int:
    global _next_worker_id
    with _workers_lock:
        wid = _next_worker_id
        _next_worker_id += 1
        stop_event = threading.Event()
        t = threading.Thread(target=_worker_loop,
                             args=(wid, stop_event),
                             name=f"worker-{wid}", daemon=True)
        _workers[wid] = {"thread": t, "stop": stop_event}
        t.start()
    return wid


def _scale_to(target: int) -> None:
    target = max(MIN_WORKERS, min(MAX_WORKERS, int(target)))
    with _workers_lock:
        live_ids = list(_workers.keys())
    current = len(live_ids)
    if target > current:
        for _ in range(target - current):
            _spawn_worker()
    elif target < current:
        for wid in sorted(live_ids, reverse=True)[:current - target]:
            with _workers_lock:
                w = _workers.get(wid)
            if w:
                w["stop"].set()


def _live_worker_count() -> int:
    with _workers_lock:
        return len(_workers)


# ---------------------------------------------------------------------
# Manager loop
# ---------------------------------------------------------------------
def _drain_queue_for_drive(drive_id: str) -> int:
    """Remove all queued file/file_delete tasks for a specific drive.
    Uses a temp list since queue.Queue doesn't support filtered pop."""
    drained = 0
    keep: list = []
    try:
        while True:
            task = _queue.get_nowait()
            if task[0] in ("file", "file_delete") and task[1] == drive_id:
                drained += 1
                _mark_consumed(task)
            else:
                keep.append(task)
    except queue.Empty:
        pass
    for t in keep:
        _queue.put(t)
    return drained


def _manager_iter(manager_conn) -> None:
    """Single iteration of the manager loop."""
    fds = db.list_fds(manager_conn)

    # Respect cancel: set flag, drain queue entries for cancelled drives
    for row in fds:
        if not row.get("enabled") and row.get("state") == "building":
            drive_id = row["drive_id"]
            if not row.get("cancel_requested"):
                db.request_cancel(manager_conn, drive_id)
                log.info("[%s] cancel requested (disabled mid-build)", drive_id)
            drained = _drain_queue_for_drive(drive_id)
            if drained:
                log.info("[%s] drained %d queued tasks", drive_id, drained)

    # Re-read after cancel writes
    fds = db.list_fds(manager_conn)

    # Enqueue new work for enabled+idle drives
    for row in fds:
        if not row.get("enabled"):
            continue
        drive_id = row["drive_id"]
        state = row.get("state")

        # Skip if already building or if queue has pending work for it
        if state == "building":
            continue
        if _pending_for_drive(drive_id) > 0:
            continue
        if db.inflight_workers_on_drive(manager_conn, drive_id) > 0:
            continue

        # Decide full vs delta
        if row.get("rotate_token"):
            _enqueue(("list_delta", drive_id))
        else:
            _enqueue(("list_full", drive_id))

    # Completion detection: for drives in state='building' with no pending
    # work and no inflight workers, enqueue finalize.
    for row in fds:
        if row.get("state") != "building":
            continue
        drive_id = row["drive_id"]
        if _pending_for_drive(drive_id) > 0:
            continue
        if db.inflight_workers_on_drive(manager_conn, drive_id) > 0:
            continue
        # total_files_listed tells us if list_task ran; without it we can't
        # say "done". But cancel + abort already cleared state, so here
        # we're safely saying "everything consumed, time to commit".
        _enqueue(("finalize", drive_id))


def main() -> int:
    signal.signal(signal.SIGINT, _signal_handler)
    try:
        signal.signal(signal.SIGTERM, _signal_handler)
    except (ValueError, AttributeError):
        pass

    try:
        lock = Lock(LOCK_PATH, stale_after_sec=1800)
        lock.acquire()
    except LockAcquireError as e:
        log.error("%s", e)
        return 2

    try:
        log.info("rag_daemon starting pid=%s (v0.4 task-queue)", os.getpid())
        db.ensure_database()
        manager_conn = db.connect()
        db.apply_global_schema(manager_conn)
        db.clear_workers(manager_conn)

        # Authenticate once; workers use thread-local services backed by these creds.
        _ = dc.authenticate()

        _scale_to(config.DAEMON_WORKER_THREADS)
        log.info("initial worker pool: %d", _live_worker_count())

        last_iter = 0.0
        last_cleanup = 0.0
        last_events_gc = 0.0
        last_liveness = 0.0

        while not _stop_flag:
            now = time.time()

            # Reconnect manager_conn if dead
            if now - last_liveness >= 10:
                if not _conn_alive(manager_conn):
                    log.warning("manager_conn dead; reconnecting...")
                    manager_conn = _replace_conn(manager_conn, label="manager")
                    if manager_conn is None:
                        for _ in range(5):
                            if _stop_flag:
                                break
                            time.sleep(1)
                        continue
                last_liveness = now

            # Maintain pool size
            target = max(MIN_WORKERS, min(MAX_WORKERS, config.DAEMON_WORKER_THREADS))
            if _live_worker_count() < target:
                _scale_to(target)

            # Manager iteration (enqueue + cancel + finalize detection) — every 5s
            if now - last_iter >= 5:
                try:
                    _manager_iter(manager_conn)
                    last_iter = now
                except Exception as e:
                    log.error("manager iter failed: %s", e)
                    try: manager_conn.rollback()
                    except Exception: pass
                    manager_conn = _replace_conn(manager_conn, label="manager")

            # Zombie cleanup — every 30s
            if now - last_cleanup >= 30:
                try:
                    gc = db.cleanup_zombies(manager_conn, stale_after_sec=300)
                    if gc["workers_removed"]:
                        log.warning("zombie cleanup: removed worker rows %s",
                                    gc["workers_removed"])
                    if gc["drives_reset"]:
                        log.warning("zombie cleanup: reset stuck drives %s",
                                    gc["drives_reset"])
                    last_cleanup = now
                except Exception as e:
                    log.error("zombie cleanup failed: %s", e)
                    try: manager_conn.rollback()
                    except Exception: pass

            # daemon_events TTL — every hour
            if now - last_events_gc >= 3600:
                try:
                    with manager_conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM public.daemon_events "
                            "WHERE ts < NOW() - INTERVAL '30 days'"
                        )
                        gone = cur.rowcount
                    manager_conn.commit()
                    if gone:
                        log.info("daemon_events TTL: deleted %d rows", gone)
                    last_events_gc = now
                except Exception as e:
                    log.warning("daemon_events TTL GC failed: %s", e)
                    try: manager_conn.rollback()
                    except Exception: pass

            # Sleep in short slices so signals / scale changes are responsive
            for _ in range(2):
                if _stop_flag:
                    break
                time.sleep(1)

        # Graceful shutdown
        log.info("shutting down; signalling %d workers", _live_worker_count())
        _global_stop.set()
        with _workers_lock:
            for w in _workers.values():
                w["stop"].set()
        deadline = time.time() + 30
        while time.time() < deadline and _live_worker_count() > 0:
            time.sleep(0.5)

        try: db.clear_workers(manager_conn)
        except Exception: pass
        manager_conn.close()
    finally:
        lock.release()
    log.info("rag_daemon stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
