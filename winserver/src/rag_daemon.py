"""RAG 常駐デーモン。

動作:
  1. 起動時に Google Drive に認証 & 共有ドライブ一覧を取得。
  2. public.fd_registry に未登録のものを追加 (enabled=FALSE)。
  3. 以降、DAEMON_ROTATE_INTERVAL_SEC ごとに:
       ON 状態の FD を巡回し、
         - rotate_token なし   -> 初回全量ビルド
         - rotate_token あり   -> Changes API で差分同期
         - failed_files ありなら再試行
  4. 中断したら safe に終了 (Ctrl+C or SIGTERM)。
"""
from __future__ import annotations

import json
import os
import signal
import sys
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

_stop_flag = False


def _signal_handler(signum, frame):
    global _stop_flag
    _stop_flag = True
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
        return True


# ---------------------------------------------------------------------
# Sync / build helpers
# ---------------------------------------------------------------------
def _register_discovered_drives(conn, service) -> None:
    """Sync fd_registry with Google's shared drive list."""
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


def _embed_and_chunks_for_file(file_info: dict, service) -> list[dict]:
    """Return a list of chunk entries ready for insert_chunks.
    Spreadsheets are fanned out per sheet."""
    mime = file_info.get("mimeType", "")
    chunks_out: list[dict] = []

    if mime == "application/vnd.google-apps.spreadsheet":
        sheets = dc.extract_spreadsheet_sheets(file_info["id"])
        for sh in sheets:
            content = sh.get("content")
            failed = sh.get("failed", False)
            gid = sh["gid"]
            name = sh["name"]
            if not content:
                # still record sheet-level metadata so the file is discoverable
                emb_vec = embed_batch([f"{file_info.get('name','')} :: {name}"])[0]
                chunks_out.append(make_chunk_entry(
                    file_info, f"[シート] {file_info.get('name','')} / {name}",
                    emb_vec, 0, sheet_gid=gid, sheet_name=name,
                    partial_content=True if failed else False))
                continue
            pieces = list(chunk_text(content, config.CHUNK_SIZE, config.CHUNK_OVERLAP))
            if not pieces:
                continue
            embs = embed_batch(pieces)
            for idx, (p, e) in enumerate(zip(pieces, embs)):
                chunks_out.append(make_chunk_entry(
                    file_info, p, e, idx, sheet_gid=gid, sheet_name=name,
                    partial_content=False))
        return chunks_out

    text, partial = dc.extract_text(service, file_info)
    if text is None:
        return []
    pieces = list(chunk_text(text, config.CHUNK_SIZE, config.CHUNK_OVERLAP))
    if not pieces:
        # metadata-only fallback
        pieces = [f"[{mime}] {file_info.get('name','')}"]
        partial = True
    embs = embed_batch(pieces)
    for idx, (p, e) in enumerate(zip(pieces, embs)):
        chunks_out.append(make_chunk_entry(
            file_info, p, e, idx, partial_content=partial and idx == len(pieces) - 1))
    return chunks_out


def _process_file(conn, schema: str, drive_id: str, file_info: dict, service) -> None:
    try:
        chunks = _embed_and_chunks_for_file(file_info, service)
        if not chunks:
            return
        db.upsert_file_chunks(conn, schema, file_info["id"], chunks)
    except Exception as e:
        log.warning("process file %s failed: %s", file_info.get("id"), e)
        db.add_failed_file(conn, drive_id, file_info["id"], str(e))
        db.log_event(conn, drive_id=drive_id, level="warn", event="file_fail",
                     message=str(e)[:300],
                     extra={"file_id": file_info.get("id"), "name": file_info.get("name")})


# ---------------------------------------------------------------------
# Full build
# ---------------------------------------------------------------------
def build_full(conn, drive_id: str, service) -> None:
    log.info("[%s] full build starting", drive_id)
    db.set_state(conn, drive_id, "building")
    db.log_event(conn, drive_id=drive_id, level="info", event="build_start", message="full build")
    schema = db.ensure_fd_schema(conn, drive_id)

    # Capture the start token BEFORE listing, so any changes during build
    # are caught by the next delta sweep.
    try:
        start_token = dc.get_changes_start_token(service, drive_id)
    except Exception as e:
        log.error("getStartPageToken failed: %s", e)
        db.set_state(conn, drive_id, "error", str(e))
        return

    try:
        files = dc.list_files_in_drive(service, drive_id)
    except Exception as e:
        log.error("list_files_in_drive failed: %s", e)
        db.set_state(conn, drive_id, "error", str(e))
        return

    # drive name for folder paths
    drive_name = ""
    try:
        row = db.get_fd(conn, drive_id)
        drive_name = (row or {}).get("name", "") or ""
    except Exception:
        pass
    files = dc.attach_folder_paths(files, drive_name=drive_name)

    total = len(files)
    log.info("[%s] %d items listed", drive_id, total)

    for i, f in enumerate(files, 1):
        if _stop_flag:
            break
        if not _min_free_bytes_ok():
            log.error("[%s] disk low, aborting build", drive_id)
            db.set_state(conn, drive_id, "error", "disk low")
            return
        _process_file(conn, schema, drive_id, f, service)
        if i % 25 == 0:
            log.info("[%s] progress %d/%d", drive_id, i, total)
            db.update_counts(conn, drive_id)

    db.set_rotate_token(conn, drive_id, start_token)
    db.update_counts(conn, drive_id)
    db.touch_sync(conn, drive_id, built=True)
    db.set_state(conn, drive_id, "idle")
    db.log_event(conn, drive_id=drive_id, level="info", event="build_done",
                 message=f"items={total}")
    log.info("[%s] full build done", drive_id)


# ---------------------------------------------------------------------
# Delta sync
# ---------------------------------------------------------------------
def sync_delta(conn, drive_id: str, service) -> None:
    row = db.get_fd(conn, drive_id)
    if not row:
        return
    token = row.get("rotate_token")
    if not token:
        # no token yet -> full build
        build_full(conn, drive_id, service)
        return

    schema = db.ensure_fd_schema(conn, drive_id)
    db.set_state(conn, drive_id, "syncing")

    try:
        changes, new_token = dc.list_changes(service, token, drive_id)
    except Exception as e:
        log.error("[%s] list_changes failed: %s", drive_id, e)
        db.set_state(conn, drive_id, "error", str(e))
        return

    # Retry previously failed files first (safe before advancing token)
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

    # Apply Changes API results
    n_upd = n_del = n_fail = 0
    for ch in changes:
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
                n_fail += 1
            continue
        if not f:
            continue
        # Resolve folder path via fallback (registry drive name)
        drive_name = (row.get("name") or "")
        f["folder_path"] = f.get("folder_path", drive_name)
        try:
            chunks = _embed_and_chunks_for_file(f, service)
            if chunks:
                db.upsert_file_chunks(conn, schema, fid, chunks)
                n_upd += 1
        except Exception as e:
            log.warning("update %s failed: %s", fid, e)
            db.add_failed_file(conn, drive_id, fid, str(e))
            n_fail += 1

    db.set_rotate_token(conn, drive_id, new_token)
    db.update_counts(conn, drive_id)
    db.touch_sync(conn, drive_id)
    db.set_state(conn, drive_id, "idle")
    db.log_event(conn, drive_id=drive_id, level="info", event="sync_done",
                 message=f"updated={n_upd} deleted={n_del} failed={n_fail}")
    if n_upd or n_del or n_fail:
        log.info("[%s] sync updated=%d deleted=%d failed=%d",
                 drive_id, n_upd, n_del, n_fail)


# ---------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------
def sweep_once(conn, service) -> None:
    db.log_event(conn, drive_id=None, level="info", event="sweep_start")
    for row in db.list_fds(conn):
        if _stop_flag:
            break
        if not row.get("enabled"):
            continue
        drive_id = row["drive_id"]
        try:
            sync_delta(conn, drive_id, service)
        except Exception as e:
            log.error("sweep %s failed: %s\n%s", drive_id, e, traceback.format_exc())
            try:
                db.set_state(conn, drive_id, "error", str(e))
            except Exception:
                pass


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
        conn = db.connect()
        db.apply_global_schema(conn)

        # Authenticate Drive
        service = dc.authenticate()
        _register_discovered_drives(conn, service)

        while not _stop_flag:
            t0 = time.time()
            try:
                sweep_once(conn, service)
            except Exception as e:
                log.error("sweep crashed: %s\n%s", e, traceback.format_exc())
            elapsed = time.time() - t0
            log.info("sweep complete in %.1fs", elapsed)
            # Sleep in small slices so Ctrl+C is responsive
            remaining = max(5, config.DAEMON_ROTATE_INTERVAL_SEC - int(elapsed))
            for _ in range(remaining):
                if _stop_flag:
                    break
                time.sleep(1)

        conn.close()
    finally:
        lock.release()
    log.info("rag_daemon stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
