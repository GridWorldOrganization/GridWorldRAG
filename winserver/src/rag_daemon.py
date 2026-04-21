"""Multi-threaded RAG daemon.

Architecture:
  main thread    : manager — authenticates, populates registry, keeps a work
                   queue of enabled FDs, enforces target worker count (1..10).
  worker threads : pull a drive_id from the queue, claim it via a PG advisory
                   lock so no other process/worker touches it, then run a
                   full/delta build. Progress is mirrored into
                   public.daemon_workers on every phase change.

Scaling is live: the control API sets daemon_config.worker_count; the manager
thread polls it and spawns/retires workers accordingly. Retiring workers
finish their current FD and then exit cleanly.
"""
from __future__ import annotations

import os
import queue
import signal
import sys
import threading
import time
import traceback
from typing import Optional

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

_work_queue: queue.Queue = queue.Queue()
_workers_lock = threading.Lock()
_workers: dict[int, dict] = {}  # wid -> {thread, stop_event, conn}
_next_worker_id = 1


def _signal_handler(signum, frame):
    global _stop_flag
    _stop_flag = True
    _global_stop.set()
    log.warning("signal %s received, draining...", signum)


# ---------------------------------------------------------------------
# Disk preflight
# ---------------------------------------------------------------------
def _min_free_bytes_ok() -> bool:
    import shutil
    try:
        drive = os.path.splitdrive(str(config.PROJECT_ROOT))[0] + "\\"
        usage = shutil.disk_usage(drive)
        return usage.free >= config.DAEMON_MIN_FREE_BYTES
    except Exception:
        return False  # fail-closed


# ---------------------------------------------------------------------
# Registry sync (Drive side -> fd_registry)
# ---------------------------------------------------------------------
def _register_discovered_drives(conn, service) -> None:
    try:
        drives = dc.list_shared_drives(service)
    except Exception as e:
        log.error("list_shared_drives failed: %s", e)
        return
    for d in drives:
        try:
            db.upsert_fd(conn, d["id"], d["name"])
        except Exception as e:
            log.error("upsert_fd %s failed: %s", d.get("id"), e)


# ---------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------
def _embed_and_chunks_for_file(file_info: dict, service) -> list[dict]:
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


def _process_file(conn, schema: str, drive_id: str, file_info: dict, service) -> None:
    try:
        chunks = _embed_and_chunks_for_file(file_info, service)
        if not chunks:
            return
        db.upsert_file_chunks(conn, schema, file_info["id"], chunks)
    except Exception as e:
        log.warning("process file %s failed: %s", file_info.get("id"), e)
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            db.add_failed_file(conn, drive_id, file_info["id"], str(e))
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        db.log_event(conn, drive_id=drive_id, level="warn", event="file_fail",
                     message=str(e)[:300],
                     extra={"file_id": file_info.get("id"), "name": file_info.get("name")})


# ---------------------------------------------------------------------
# Full build & delta sync — both accept worker_id for live progress
# ---------------------------------------------------------------------
def _hb(conn, worker_id: Optional[int], **fields):
    """heartbeat — no-op when worker_id is None."""
    if worker_id is None:
        return
    try:
        db.heartbeat_worker(conn, worker_id, **fields)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def build_full(conn, drive_id: str, service, *, worker_id: Optional[int] = None) -> None:
    row = db.get_fd(conn, drive_id)
    drive_name = (row or {}).get("name", "") or ""
    log.info("[%s] (w#%s) full build starting", drive_id, worker_id)
    db.set_state(conn, drive_id, "building")
    db.log_event(conn, drive_id=drive_id, level="info", event="build_start", message="full build")
    _hb(conn, worker_id, state="listing", phase="getting_start_token",
        drive_id=drive_id, drive_name=drive_name,
        current_file=None, files_done=0, total_files=0,
        started_at="now()" and None)

    try:
        start_token = dc.get_changes_start_token(service, drive_id)
    except Exception as e:
        log.error("getStartPageToken failed: %s", e)
        db.set_state(conn, drive_id, "error", str(e))
        return

    schema = db.ensure_fd_schema(conn, drive_id)

    _hb(conn, worker_id, state="listing", phase="listing_files",
        drive_id=drive_id, drive_name=drive_name)
    try:
        files = dc.list_files_in_drive(service, drive_id)
    except Exception as e:
        log.error("list_files_in_drive failed: %s", e)
        db.set_state(conn, drive_id, "error", str(e))
        return
    files = dc.attach_folder_paths(files, drive_name=drive_name)
    total = len(files)
    log.info("[%s] (w#%s) %d items", drive_id, worker_id, total)
    _hb(conn, worker_id, state="building", phase="processing_file",
        total_files=total, files_done=0)

    for i, f in enumerate(files, 1):
        if _stop_flag:
            break
        if not _min_free_bytes_ok():
            log.error("[%s] disk low, aborting", drive_id)
            db.set_state(conn, drive_id, "error", "disk low")
            return
        _hb(conn, worker_id, files_done=i - 1, current_file=f.get("name", ""))
        _process_file(conn, schema, drive_id, f, service)
        if i % 25 == 0:
            try:
                db.update_counts(conn, drive_id)
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass

    db.set_rotate_token(conn, drive_id, start_token)
    try:
        db.update_counts(conn, drive_id)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    db.touch_sync(conn, drive_id, built=True)
    db.set_state(conn, drive_id, "idle")
    db.log_event(conn, drive_id=drive_id, level="info", event="build_done",
                 message=f"items={total}")
    _hb(conn, worker_id, state="done", phase=None,
        files_done=total, total_files=total, current_file=None)
    log.info("[%s] (w#%s) full build done", drive_id, worker_id)


def sync_delta(conn, drive_id: str, service, *, worker_id: Optional[int] = None) -> None:
    row = db.get_fd(conn, drive_id)
    if not row:
        return
    token = row.get("rotate_token")
    if not token:
        build_full(conn, drive_id, service, worker_id=worker_id)
        return

    drive_name = row.get("name", "") or ""
    schema = db.ensure_fd_schema(conn, drive_id)
    db.set_state(conn, drive_id, "syncing")
    _hb(conn, worker_id, state="syncing", phase="fetching_changes",
        drive_id=drive_id, drive_name=drive_name,
        current_file=None, files_done=0, total_files=0)

    try:
        changes, new_token = dc.list_changes(service, token, drive_id)
    except Exception as e:
        log.error("[%s] list_changes failed: %s", drive_id, e)
        db.set_state(conn, drive_id, "error", str(e))
        return

    # Retry failed_files first
    failed = db.get_failed_files(conn, drive_id)
    for entry in list(failed):
        if _stop_flag:
            break
        fid = entry.get("file_id")
        if not fid:
            db.clear_failed_file(conn, drive_id, fid or "")
            continue
        info = dc.get_file_info(service, fid)
        if info is None or info.get("trashed"):
            db.clear_failed_file(conn, drive_id, fid)
            continue
        try:
            chunks = _embed_and_chunks_for_file(info, service)
            if chunks:
                db.upsert_file_chunks(conn, schema, fid, chunks)
            db.clear_failed_file(conn, drive_id, fid)
        except Exception as e:
            log.warning("retry %s still failing: %s", fid, e)

    _hb(conn, worker_id, phase="processing_file", total_files=len(changes), files_done=0)
    n_upd = n_del = n_fail = 0
    for i, ch in enumerate(changes, 1):
        if _stop_flag:
            break
        fid = ch.get("fileId")
        f = ch.get("file")
        if ch.get("removed") or (f and f.get("trashed")):
            try:
                db.delete_by_file_id(conn, schema, fid)
                n_del += 1
            except Exception as e:
                log.warning("delete %s failed: %s", fid, e)
                try:
                    conn.rollback()
                except Exception:
                    pass
                n_fail += 1
            continue
        if not f:
            continue
        f["folder_path"] = f.get("folder_path", drive_name)
        _hb(conn, worker_id, files_done=i - 1, current_file=f.get("name", ""))
        try:
            chunks = _embed_and_chunks_for_file(f, service)
            if chunks:
                db.upsert_file_chunks(conn, schema, fid, chunks)
                n_upd += 1
        except Exception as e:
            log.warning("update %s failed: %s", fid, e)
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                db.add_failed_file(conn, drive_id, fid, str(e))
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
            n_fail += 1

    db.set_rotate_token(conn, drive_id, new_token)
    try:
        db.update_counts(conn, drive_id)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    db.touch_sync(conn, drive_id)
    db.set_state(conn, drive_id, "idle")
    db.log_event(conn, drive_id=drive_id, level="info", event="sync_done",
                 message=f"updated={n_upd} deleted={n_del} failed={n_fail}")
    _hb(conn, worker_id, state="done", phase=None, current_file=None)


# ---------------------------------------------------------------------
# Worker thread lifecycle
# ---------------------------------------------------------------------
def _worker_loop(wid: int, personal_stop: threading.Event) -> None:
    """One worker thread. Gets its own DB connection and its own Drive
    service (via dc.get_drive_service()'s thread-local cache)."""
    conn = None
    try:
        conn = db.connect()
        db.heartbeat_worker(conn, wid, state="idle",
                            drive_id=None, drive_name=None, current_file=None,
                            files_done=0, total_files=0, phase=None, last_error=None)
        service = dc.get_drive_service()
        log.info("worker %d started", wid)

        while not _global_stop.is_set() and not personal_stop.is_set():
            try:
                drive_id = _work_queue.get(timeout=2.0)
            except queue.Empty:
                db.heartbeat_worker(conn, wid, state="idle",
                                    drive_id=None, drive_name=None, current_file=None,
                                    files_done=0, total_files=0, phase=None)
                continue

            # Try to claim the drive exclusively across all workers + processes.
            if not db.try_claim_drive(conn, drive_id):
                log.info("worker %d: drive %s already claimed, skipping", wid, drive_id)
                continue
            try:
                db.heartbeat_worker(conn, wid, state="claiming",
                                    drive_id=drive_id, drive_name=None,
                                    current_file=None, files_done=0, total_files=0,
                                    phase=None, last_error=None)
                try:
                    sync_delta(conn, drive_id, service, worker_id=wid)
                except Exception as e:
                    log.error("worker %d failed %s: %s\n%s",
                              wid, drive_id, e, traceback.format_exc())
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    try:
                        db.set_state(conn, drive_id, "error", str(e))
                    except Exception:
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                    try:
                        db.heartbeat_worker(conn, wid, state="error", last_error=str(e)[:500])
                    except Exception:
                        pass
            finally:
                try:
                    db.release_drive(conn, drive_id)
                except Exception:
                    pass
    finally:
        if conn is not None:
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM public.daemon_workers WHERE worker_id=%s", (wid,))
                conn.commit()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
        with _workers_lock:
            _workers.pop(wid, None)
        log.info("worker %d exited", wid)


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


def _scale_to(target: int) -> tuple[int, int]:
    """Adjust worker pool to `target`. Returns (spawned, retired)."""
    target = max(MIN_WORKERS, min(MAX_WORKERS, int(target)))
    with _workers_lock:
        live_ids = list(_workers.keys())
    spawned = retired = 0
    current = len(live_ids)
    if target > current:
        for _ in range(target - current):
            _spawn_worker()
            spawned += 1
    elif target < current:
        # Retire highest-numbered workers first.
        for wid in sorted(live_ids, reverse=True)[:current - target]:
            with _workers_lock:
                w = _workers.get(wid)
            if w:
                w["stop"].set()
                retired += 1
    return spawned, retired


def _live_worker_count() -> int:
    with _workers_lock:
        return len(_workers)


# ---------------------------------------------------------------------
# Manager main loop
# ---------------------------------------------------------------------
def _enqueue_pending_work(conn) -> int:
    """Put each enabled FD that is not currently being worked on into the queue.

    Already-queued duplicates are harmless — the worker's advisory lock will
    cause the duplicate pickup to skip.
    """
    try:
        active = db.active_drive_ids(conn)
    except Exception:
        active = set()
    fds = db.list_fds(conn)
    n = 0
    for r in fds:
        if not r.get("enabled"):
            continue
        if r["drive_id"] in active:
            continue
        _work_queue.put(r["drive_id"])
        n += 1
    return n


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
        log.info("rag_daemon starting pid=%s", os.getpid())
        db.ensure_database()
        manager_conn = db.connect()
        db.apply_global_schema(manager_conn)
        db.clear_workers(manager_conn)  # wipe stale rows from last run

        # Seed target worker count if not set.
        if db.get_config(manager_conn, "worker_count") is None:
            db.set_config(manager_conn, "worker_count", str(config.DAEMON_WORKER_THREADS))

        # Authenticate once (this refreshes creds, seeds dc._credentials).
        main_service = dc.authenticate()
        _register_discovered_drives(manager_conn, main_service)

        # Initial scale.
        try:
            initial = int(db.get_config(manager_conn, "worker_count",
                                        str(config.DAEMON_WORKER_THREADS)))
        except (TypeError, ValueError):
            initial = config.DAEMON_WORKER_THREADS
        _scale_to(initial)
        log.info("initial worker pool size: %d", _live_worker_count())

        last_enqueue = 0.0
        last_cleanup = 0.0
        while not _stop_flag:
            # Poll target worker count
            try:
                target_s = db.get_config(manager_conn, "worker_count",
                                         str(config.DAEMON_WORKER_THREADS))
                target = int(target_s) if target_s else config.DAEMON_WORKER_THREADS
            except Exception:
                target = config.DAEMON_WORKER_THREADS
            target = max(MIN_WORKERS, min(MAX_WORKERS, target))
            current = _live_worker_count()
            if target != current:
                sp, rt = _scale_to(target)
                log.info("scale: current=%d -> target=%d (+%d/-%d)",
                         current, target, sp, rt)

            now = time.time()
            # Garbage-collect zombie worker rows + stuck fd_registry.state.
            # Runs every 30s — cheap query, cap on blast radius.
            if now - last_cleanup >= 30:
                try:
                    gc = db.cleanup_zombies(manager_conn, stale_after_sec=90)
                    if gc["workers_removed"]:
                        log.warning("zombie cleanup: removed worker rows %s",
                                    gc["workers_removed"])
                    if gc["drives_reset"]:
                        log.warning("zombie cleanup: reset stuck drives %s",
                                    gc["drives_reset"])
                    last_cleanup = now
                except Exception as e:
                    log.error("zombie cleanup failed: %s", e)
                    try:
                        manager_conn.rollback()
                    except Exception:
                        pass

            if now - last_enqueue >= config.DAEMON_ROTATE_INTERVAL_SEC:
                try:
                    n = _enqueue_pending_work(manager_conn)
                    if n:
                        log.info("manager: enqueued %d FD(s)", n)
                    last_enqueue = now
                except Exception as e:
                    log.error("enqueue failed: %s", e)
                    try:
                        manager_conn.rollback()
                    except Exception:
                        pass

            # Short sleep so signals & scale changes are responsive.
            for _ in range(3):
                if _stop_flag:
                    break
                time.sleep(1)

        # Shutdown: signal all workers, wait up to 30s.
        log.info("shutting down; signalling %d workers", _live_worker_count())
        _global_stop.set()
        with _workers_lock:
            for w in _workers.values():
                w["stop"].set()
        deadline = time.time() + 30
        while time.time() < deadline and _live_worker_count() > 0:
            time.sleep(0.5)

        try:
            db.clear_workers(manager_conn)
        except Exception:
            pass
        manager_conn.close()
    finally:
        lock.release()
    log.info("rag_daemon stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
