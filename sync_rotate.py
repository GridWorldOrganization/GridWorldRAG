#!/usr/bin/env python3
"""
sync_rotate.py - 共有ドライブ単位のローテーション型差分同期。

ドライブごとに独立したページトークンを保持し、5分間隔の頻回実行で
「変更があった共有ドライブだけサクッと処理」する。

使い方:
    python sync_rotate.py                  # 全ドライブの変更を一巡チェック
    python sync_rotate.py --init           # 全ドライブのトークン初期化
    python sync_rotate.py --db N           # DB番号指定
    python sync_rotate.py --drive <id>     # 特定ドライブのみ処理

launchd (LaunchAgent) から 5 分間隔で呼び出す想定。
多重起動防止に /tmp/gridworldrag_rotate.lock を使う。
"""

import argparse
import json
import logging
import os
import shutil
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import psycopg2

from src.config import (
    EMBEDDING_MODEL, EMBEDDING_DEVICE, CHUNK_SIZE, CHUNK_OVERLAP,
    load_shared_drives_whitelist,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    TELEGRAM_RETRY_PENDING_THRESHOLD, TELEGRAM_NOTIFY_COOLDOWN_SEC,
)
from src.drive_client import (
    authenticate,
    get_changes_start_token,
    list_changes,
    extract_text,
    extract_spreadsheet_sheets,
    resolve_folder_path_api,
    SKIP_MIME_TYPES,
    _api_call_with_retry,
)
from src.db import connect, upsert_file_chunks, delete_by_file_id
from src.indexer import make_chunk_entry

LOCK_FILE = Path("/tmp/gridworldrag_rotate.lock")
_STALE_LOCK_SEC = 1200  # 20分以上古いロックは stale 扱い

# 空き容量チェックの閾値 (1GB)
MIN_FREE_BYTES = 1 * 1024 * 1024 * 1024

# ログ出力先: macOS 慣例に従い ~/Library/Logs/ を使用
LOG_DIR = Path.home() / "Library" / "Logs" / "gridworldrag"
LOG_FILE = LOG_DIR / "sync_rotate.log"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5MB ごとにローテート
LOG_BACKUP_COUNT = 3             # 3世代保持

log = logging.getLogger("sync_rotate")


class DiskFullHalt(Exception):
    """ディスク満杯を検出したため処理全体を中止するシグナル。"""


def _setup_logging():
    """RotatingFileHandler + StreamHandler でロガーを構成する。"""
    if log.handlers:
        return  # 二重初期化防止（テストから呼ばれた時など）

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        # ログディレクトリが作れなくても stderr にフォールバック
        pass

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 標準出力（launchd の StandardOutPath 経由でも残る）
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    log.addHandler(stream)

    # ファイル（ローテート付き）
    if LOG_DIR.exists():
        try:
            rotating = RotatingFileHandler(
                LOG_FILE,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            rotating.setFormatter(fmt)
            log.addHandler(rotating)
        except OSError:
            pass  # ディスク満杯などで作れなくても stream は残る

    log.setLevel(logging.INFO)
    log.propagate = False


def _acquire_lock():
    """多重起動防止。PID liveness probe で死んだロックを即 takeover する。

    判定順:
    1. lockfile に書かれた PID を os.kill(pid, 0) で probe
       - ProcessLookupError → 前回プロセスは終了済み、takeover
       - PermissionError    → 他ユーザーのプロセスが生きている可能性、exit(0)
       - 成功               → 生存中、exit(0)
    2. PID が読めない (破損/空) → mtime ベースの fallback (20分 stale)
    """
    if LOCK_FILE.exists():
        try:
            content = LOCK_FILE.read_text().strip()
            prev_pid = int(content) if content else None
        except (OSError, ValueError):
            prev_pid = None

        if prev_pid is not None:
            try:
                os.kill(prev_pid, 0)
            except ProcessLookupError:
                log.info("stale lock (pid=%d は終了済み) を引き継ぎ", prev_pid)
                try:
                    LOCK_FILE.unlink()
                except FileNotFoundError:
                    pass
            except PermissionError:
                log.info("前回実行中 (pid=%d, 他ユーザー) スキップ", prev_pid)
                sys.exit(0)
            else:
                log.info("前回実行中 (pid=%d) スキップ", prev_pid)
                sys.exit(0)
        else:
            # PID 不明: mtime fallback
            try:
                age = time.time() - LOCK_FILE.stat().st_mtime
            except FileNotFoundError:
                age = _STALE_LOCK_SEC + 1
            if age <= _STALE_LOCK_SEC:
                log.info("前回実行中 (pid不明, age=%ds) スキップ", int(age))
                sys.exit(0)
            log.info("stale lock (pid不明, age=%ds) を引き継ぎ", int(age))
            try:
                LOCK_FILE.unlink()
            except FileNotFoundError:
                pass
    LOCK_FILE.write_text(str(os.getpid()))


def _release_lock():
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# sync_state テーブル ヘルパー
# ---------------------------------------------------------------------------

_SYNC_STATE_DDL = """
CREATE TABLE IF NOT EXISTS sync_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS sync_history (
    id                  SERIAL PRIMARY KEY,
    ran_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    duration_ms         INTEGER,
    drives_checked      INTEGER,
    drives_with_changes INTEGER,
    added               INTEGER,
    updated             INTEGER,
    deleted             INTEGER,
    skipped             INTEGER,
    errors              INTEGER,
    retry_recovered     INTEGER,
    retry_pending       INTEGER,
    dead_files_total    INTEGER,
    disk_full           BOOLEAN DEFAULT FALSE,
    aborted             BOOLEAN DEFAULT FALSE,
    reason              TEXT,
    free_bytes          BIGINT
);
CREATE INDEX IF NOT EXISTS idx_sync_history_ran_at ON sync_history (ran_at DESC);
"""
_TOKEN_PREFIX = "rotate_token_"
_RESULT_KEY = "last_sync_result"
_FAILED_FILES_KEY = "failed_files"
_DEAD_FILES_KEY = "dead_files"

# issue #9: 再試行キューの諦めロジック
# attempts >= MAX_ATTEMPTS または age >= MAX_AGE_SEC になったら dead_files に移動
MAX_RETRY_ATTEMPTS = int(os.environ.get("RETRY_MAX_ATTEMPTS", "5"))
MAX_RETRY_AGE_SEC = int(os.environ.get("RETRY_MAX_AGE_SEC", str(24 * 3600)))  # 24h


def _ensure_sync_state_table(conn):
    cur = conn.cursor()
    try:
        cur.execute(_SYNC_STATE_DDL)
        conn.commit()
    finally:
        cur.close()


def _load_tokens(conn, drive_ids):
    """複数ドライブ分のトークンを 1 クエリで取得する。"""
    keys = [_TOKEN_PREFIX + d for d in drive_ids]
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT key, value FROM sync_state WHERE key = ANY(%s)",
            (keys,),
        )
        rows = dict(cur.fetchall())
    finally:
        cur.close()
    return {d: rows.get(_TOKEN_PREFIX + d) for d in drive_ids}


def _save_token(conn, drive_id, token):
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO sync_state (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at = NOW()
            """,
            (_TOKEN_PREFIX + drive_id, token),
        )
        conn.commit()
    finally:
        cur.close()


def _save_sync_result(conn, result):
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO sync_state (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at = NOW()
            """,
            (_RESULT_KEY, json.dumps(result, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        cur.close()
    # sync_history append (issue #8)
    _save_sync_history(conn, result)


def _save_sync_history(conn, result):
    """sync_history テーブルに 1 実行 1 行を追記する (append-only)。"""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO sync_history (
                duration_ms, drives_checked, drives_with_changes,
                added, updated, deleted, skipped, errors,
                retry_recovered, retry_pending, dead_files_total,
                disk_full, aborted, reason, free_bytes
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                result.get("duration_ms"),
                result.get("drives_checked", 0),
                result.get("drives_with_changes", 0),
                len(result.get("added", [])) if isinstance(result.get("added"), list) else result.get("added", 0),
                len(result.get("updated", [])) if isinstance(result.get("updated"), list) else result.get("updated", 0),
                len(result.get("deleted", [])) if isinstance(result.get("deleted"), list) else result.get("deleted", 0),
                result.get("skipped", 0),
                result.get("errors", 0),
                result.get("retry_recovered", 0),
                result.get("retry_pending", 0),
                result.get("dead_files_total", 0),
                bool(result.get("disk_full")),
                bool(result.get("aborted")),
                result.get("reason"),
                result.get("free_bytes"),
            ),
        )
        conn.commit()
    except Exception as e:
        log.warning("sync_history 追記失敗: %s", e)
    finally:
        cur.close()


def _load_failed_files(conn):
    """再試行待ちのファイルIDリストを取得する。"""
    cur = conn.cursor()
    try:
        cur.execute("SELECT value FROM sync_state WHERE key = %s", (_FAILED_FILES_KEY,))
        row = cur.fetchone()
    finally:
        cur.close()
    if not row:
        return []
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        return []


def _save_failed_files(conn, failed):
    """再試行待ちのファイルIDリストを保存する。"""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO sync_state (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at = NOW()
            """,
            (_FAILED_FILES_KEY, json.dumps(failed, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        cur.close()


def _load_dead_files(conn):
    """永続失敗ファイル (再試行しない) の一覧を取得する。"""
    cur = conn.cursor()
    try:
        cur.execute("SELECT value FROM sync_state WHERE key = %s", (_DEAD_FILES_KEY,))
        row = cur.fetchone()
    finally:
        cur.close()
    if not row:
        return []
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        return []


def _save_dead_files(conn, dead):
    """永続失敗ファイルの一覧を保存する。"""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO sync_state (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at = NOW()
            """,
            (_DEAD_FILES_KEY, json.dumps(dead, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        cur.close()


def _promote_to_dead(conn, entry, reason):
    """失敗エントリを dead_files に昇格させる (永続失敗扱い)。"""
    dead = _load_dead_files(conn)
    entry_with_reason = dict(entry)
    entry_with_reason["dead_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    entry_with_reason["dead_reason"] = reason
    dead.append(entry_with_reason)
    # dead_files のサイズが肥大化しないよう上限を設ける (最新 500 件)
    if len(dead) > 500:
        dead = dead[-500:]
    _save_dead_files(conn, dead)
    log.warning("dead file (永続失敗): file_id=%s reason=%s", entry.get("file_id", ""), reason)


# ---------------------------------------------------------------------------
# Telegram 通知 (issue #7)
# ---------------------------------------------------------------------------

_TELEGRAM_NOTIFY_STATE_KEY = "telegram_last_notify"


def _telegram_notify(conn, message):
    """Telegram bot API に直接 HTTPS リクエストを送る。

    config.env の TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID が設定されていない場合はスキップ。
    バッチ過程から呼ばれるため、失敗しても sync を妨げない (best effort)。
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        import urllib.request
        import urllib.parse
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message[:4000],  # Telegram の msg 上限は 4096
            "parse_mode": "Markdown",
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        log.warning("Telegram 通知失敗: %s", e)
        return False


def _maybe_notify_retry_pending(conn, retry_pending, dead_count):
    """retry_pending 数がしきい値を超えたら Telegram 通知を送る。

    クールダウン期間内は重複通知しない。
    """
    if retry_pending < TELEGRAM_RETRY_PENDING_THRESHOLD:
        return
    if not TELEGRAM_BOT_TOKEN:
        return

    # 前回通知時刻をチェック
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT value FROM sync_state WHERE key = %s",
            (_TELEGRAM_NOTIFY_STATE_KEY,),
        )
        row = cur.fetchone()
    finally:
        cur.close()

    now = time.time()
    if row:
        try:
            last = float(row[0])
            if now - last < TELEGRAM_NOTIFY_COOLDOWN_SEC:
                log.info("Telegram 通知スキップ (クールダウン中)")
                return
        except (ValueError, TypeError):
            pass

    # 失敗上位ファイル情報
    failed = _load_failed_files(conn)[:5]
    failed_lines = []
    for e in failed:
        fid = e.get("file_id", "?")[:16]
        attempts = e.get("attempts", 0)
        err = (e.get("last_error") or "")[:80]
        failed_lines.append(f"  • `{fid}` (attempts={attempts})")
        if err:
            failed_lines.append(f"    {err}")

    msg = (
        f"⚠️ *GridWorldRAG sync_rotate*\n"
        f"retry\\_pending: {retry_pending} (threshold {TELEGRAM_RETRY_PENDING_THRESHOLD})\n"
        f"dead\\_files: {dead_count}\n"
        f"\n"
        f"failed files (top {len(failed)}):\n"
        + "\n".join(failed_lines)
    )
    ok = _telegram_notify(conn, msg)
    if ok:
        # 通知成功 → クールダウン開始
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO sync_state (key, value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                """,
                (_TELEGRAM_NOTIFY_STATE_KEY, str(now)),
            )
            conn.commit()
        finally:
            cur.close()
        log.info("Telegram 通知送信完了")


def _entry_should_be_dead(entry):
    """エントリが dead_files に移動すべきかを判定する。

    Returns:
        (should_die: bool, reason: str)
    """
    attempts = entry.get("attempts", 0)
    if attempts >= MAX_RETRY_ATTEMPTS:
        return True, f"attempts exceeded ({attempts} >= {MAX_RETRY_ATTEMPTS})"
    first_failed_at = entry.get("first_failed_at")
    if first_failed_at:
        try:
            first_ts = time.mktime(time.strptime(first_failed_at, "%Y-%m-%dT%H:%M:%S"))
            age = time.time() - first_ts
            if age >= MAX_RETRY_AGE_SEC:
                return True, f"age exceeded ({int(age)}s >= {MAX_RETRY_AGE_SEC}s)"
        except (ValueError, TypeError):
            pass
    return False, ""


# ---------------------------------------------------------------------------
# ディスク容量チェック
# ---------------------------------------------------------------------------

def _check_disk_space(path=None):
    """PG データディレクトリの空き容量を返す。

    Returns:
        (ok: bool, free_bytes: int, total_bytes: int)
    """
    if path is None:
        # PostgreSQL@17 の標準パス。存在しなければ '/' にフォールバック
        path = "/opt/homebrew/var/postgresql@17"
        if not Path(path).exists():
            path = "/"
    try:
        usage = shutil.disk_usage(path)
    except Exception:
        # 計測できない場合は失敗を返す（安全側）
        return False, 0, 0
    return usage.free >= MIN_FREE_BYTES, usage.free, usage.total


def _is_disk_full_error(exc):
    """例外が DB のディスク満杯系か判定する。"""
    if isinstance(exc, psycopg2.errors.DiskFull):  # psycopg2 2.9+
        return True
    msg = str(exc).lower()
    for needle in ("no space", "disk full", "out of space", "space left"):
        if needle in msg:
            return True
    return False


# ---------------------------------------------------------------------------
# チャンク生成
# ---------------------------------------------------------------------------

def _build_chunks(file_info, service, model, splitter):
    """1ファイルからチャンクエントリのリストを生成する。"""
    mime = file_info.get("mimeType", "")

    if mime == "application/vnd.google-apps.spreadsheet":
        sheets = extract_spreadsheet_sheets(file_info["id"])
        if not sheets:
            return []
        chunks = []
        for sheet in sheets:
            is_partial = sheet.get("failed", False)
            content = sheet.get("content")
            sheet_text = (
                f"[シート: {sheet['name']}]\n{content}"
                if content
                else f"[シート: {sheet['name']}]"
            )
            text_chunks = splitter.split_text(sheet_text) or [sheet_text]
            try:
                embeddings = model.encode(text_chunks)
            except Exception as e:
                log.warning("embed失敗 [%s/%s]: %s", file_info.get("name", "?"), sheet["name"], e)
                continue
            for ci, (chunk_text, emb) in enumerate(zip(text_chunks, embeddings)):
                chunks.append(make_chunk_entry(
                    file_info, chunk_text, emb, ci,
                    sheet_gid=sheet["gid"], sheet_name=sheet["name"],
                    partial_content=is_partial,
                ))
        return chunks

    text, is_partial = extract_text(service, file_info)
    if not text or not text.strip():
        return []

    text_chunks = splitter.split_text(text)
    if not text_chunks:
        return []

    embeddings = model.encode(text_chunks)
    return [
        make_chunk_entry(file_info, chunk_text, emb, ci, partial_content=is_partial)
        for ci, (chunk_text, emb) in enumerate(zip(text_chunks, embeddings))
    ]


# ---------------------------------------------------------------------------
# 1ドライブ分の変更処理
# ---------------------------------------------------------------------------

def _process_drive_changes(service, conn, model, splitter, drive_id, changes):
    """1ドライブの Changes API 結果を処理する。

    Raises:
        DiskFullHalt: DB がディスク満杯を検出した場合。呼び出し側は直ちに中止すべき。

    Returns:
        (added, updated, deleted, errors, skipped, failed_ids)
        failed_ids は再試行キューに追加すべき file_id のリスト。
    """
    added, updated, deleted = [], [], []
    failed_ids = []
    errors = skipped = 0
    folder_cache = {}

    # 同一 file_id に対する重複変更は最新 1 件のみ保持
    latest = {}
    for change in changes:
        fid = change.get("fileId")
        if isinstance(fid, str):
            latest[fid] = change

    for file_id, change in latest.items():
        is_removed = change.get("removed", False)
        file_info = change.get("file")

        # 削除・ゴミ箱
        if is_removed or (file_info and file_info.get("trashed")):
            try:
                n = delete_by_file_id(conn, file_id)
            except Exception as e:
                if _is_disk_full_error(e):
                    raise DiskFullHalt(f"DB ディスク満杯 (削除時): {e}") from e
                log.warning("削除失敗 [%s]: %s", file_id, e)
                errors += 1
                continue
            if n > 0:
                fname = (file_info or {}).get("name", file_id)
                furl = (file_info or {}).get("webViewLink", "")
                log.info("削除: %s", fname)
                deleted.append({"name": fname, "url": furl, "id": file_id})
            continue

        if file_info is None:
            skipped += 1
            continue

        # 当該ドライブ外の変更は除外
        if file_info.get("driveId") != drive_id:
            skipped += 1
            continue

        fname = file_info.get("name", "?")
        mime = file_info.get("mimeType", "")
        furl = file_info.get("webViewLink", "")

        if mime in SKIP_MIME_TYPES:
            skipped += 1
            continue

        file_info["folder_path"] = resolve_folder_path_api(
            service, file_info, folder_cache
        )

        try:
            chunks = _build_chunks(file_info, service, model, splitter)
        except Exception as e:
            log.error("chunk生成エラー: %s (%s)", fname, e)
            errors += 1
            failed_ids.append({
                "drive_id": drive_id,
                "file_id": file_id,
                "attempts": 1,
                "first_failed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "last_failed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            continue

        if not chunks:
            skipped += 1
            continue

        try:
            status = upsert_file_chunks(conn, file_id, chunks)
        except Exception as e:
            if _is_disk_full_error(e):
                raise DiskFullHalt(f"DB ディスク満杯 [{fname}]: {e}") from e
            log.warning("DB書込失敗 [%s]: %s", fname, e)
            errors += 1
            failed_ids.append({
                "drive_id": drive_id,
                "file_id": file_id,
                "attempts": 1,
                "first_failed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "last_failed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            continue

        entry = {"name": fname, "url": furl, "id": file_id}
        if status == "updated":
            log.info("更新: %s", fname)
            updated.append(entry)
        else:
            log.info("追加: %s", fname)
            added.append(entry)

    return added, updated, deleted, errors, skipped, failed_ids


# ---------------------------------------------------------------------------
# メインループ
# ---------------------------------------------------------------------------

def _get_model_and_splitter():
    """SentenceTransformer と splitter を遅延ロードする。"""
    from sentence_transformers import SentenceTransformer
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    log.info("モデルロード中...")
    model = SentenceTransformer(EMBEDDING_MODEL, device=(None if EMBEDDING_DEVICE == "auto" else EMBEDDING_DEVICE))
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP,
    )
    log.info("モデルロード完了")
    return model, splitter


def _retry_failed_files(service, conn, model_getter):
    """前回失敗したファイルを再処理する。

    Args:
        model_getter: モデルを遅延取得する callable (引数なし) 。
                      返り値は (model, splitter)。

    Returns:
        (recovered, still_failed, errors, disk_full)
        still_failed は再び失敗したエントリの list、disk_full は True なら halt 要求。
    """
    queue = _load_failed_files(conn)
    if not queue:
        return [], [], 0, False

    log.info("再試行キュー: %d件", len(queue))

    still_failed = []
    recovered = []
    errors = 0
    dead_count = 0
    model = splitter = None

    def _fail_entry(entry, err_msg):
        """エントリを失敗扱いにし、しきい値を超えたら dead_files に移動する"""
        nonlocal dead_count
        e2 = dict(entry)
        e2["attempts"] = e2.get("attempts", 0) + 1
        e2["last_failed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        e2["last_error"] = (err_msg or "")[:500]
        if "first_failed_at" not in e2:
            e2["first_failed_at"] = e2["last_failed_at"]
        should_die, reason = _entry_should_be_dead(e2)
        if should_die:
            _promote_to_dead(conn, e2, reason)
            dead_count += 1
        else:
            still_failed.append(e2)

    for entry in queue:
        drive_id = entry.get("drive_id", "")
        file_id = entry.get("file_id", "")
        if not file_id:
            continue

        try:
            file_info = _api_call_with_retry(lambda: service.files().get(
                fileId=file_id,
                fields=(
                    "id,name,mimeType,modifiedTime,trashed,owners,"
                    "webViewLink,driveId,parents,"
                    "permissions(emailAddress,role,type,displayName)"
                ),
                supportsAllDrives=True,
            ).execute())
        except Exception as e:
            # files().get() で 404 が返ったら即 dead (ファイルが削除された)
            if "404" in str(e):
                _promote_to_dead(conn, entry, "404 not found")
                dead_count += 1
                continue
            log.warning("再取得失敗 [%s]: %s", file_id, e)
            _fail_entry(entry, str(e))
            errors += 1
            continue

        if file_info.get("trashed"):
            # ゴミ箱へ移動済み: DB から消して成功扱い
            try:
                delete_by_file_id(conn, file_id)
                recovered.append(file_id)
            except Exception as e:
                if _is_disk_full_error(e):
                    still_failed.append(entry)
                    still_failed.extend([
                        q for q in queue
                        if q.get("file_id") not in recovered
                        and q.get("file_id") != file_id
                    ])
                    _save_failed_files(conn, still_failed)
                    return recovered, still_failed, errors, True
                log.warning("再試行削除失敗 [%s]: %s", file_id, e)
                _fail_entry(entry, str(e))
                errors += 1
            continue

        if model is None:
            model, splitter = model_getter()

        try:
            file_info["folder_path"] = resolve_folder_path_api(service, file_info, {})
            chunks = _build_chunks(file_info, service, model, splitter)
        except Exception as e:
            log.warning("再試行 chunk 生成失敗 [%s]: %s", file_id, e)
            _fail_entry(entry, str(e))
            errors += 1
            continue

        if not chunks:
            recovered.append(file_id)
            continue

        try:
            upsert_file_chunks(conn, file_id, chunks)
            log.info("再試行成功: %s", file_info.get("name", file_id))
            recovered.append(file_id)
        except Exception as e:
            if _is_disk_full_error(e):
                # 残りはすべて未処理として保存
                still_failed.append(entry)
                still_failed.extend([
                    q for q in queue
                    if q.get("file_id") not in recovered
                    and q.get("file_id") != file_id
                ])
                _save_failed_files(conn, still_failed)
                return recovered, still_failed, errors, True
            log.warning("再試行 upsert 失敗 [%s]: %s", file_id, e)
            _fail_entry(entry, str(e))
            errors += 1

    _save_failed_files(conn, still_failed)
    if dead_count > 0:
        log.warning("dead files に移動: %d件", dead_count)
    return recovered, still_failed, errors, False


def _run(args):
    _setup_logging()

    whitelist = load_shared_drives_whitelist()
    if not whitelist:
        log.warning("ホワイトリストが空です")
        return

    # 事前の空き容量チェック（PG データディレクトリ）
    ok, free, total = _check_disk_space()
    if not ok and not args.init:
        log.error(
            "空き容量不足: free=%.2fGB total=%.2fGB 閾値=%.2fGB 処理を中断",
            free / 1024**3, total / 1024**3, MIN_FREE_BYTES / 1024**3,
        )
        # 結果を記録してから退出（トークンは進めない）
        try:
            conn = connect()
            _ensure_sync_state_table(conn)
            _save_sync_result(conn, {
                "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "aborted": True,
                "reason": "disk_full_preflight",
                "free_bytes": free,
                "total_bytes": total,
            })
            conn.close()
        except Exception:
            pass
        sys.exit(2)

    log.info("Drive認証中...")
    service = authenticate()
    log.info("Drive認証完了")

    conn = connect()
    _ensure_sync_state_table(conn)

    # --init: 全ドライブのトークンを取得して保存
    if args.init:
        for drive_id in whitelist:
            try:
                token = get_changes_start_token(service, drive_id=drive_id)
                _save_token(conn, drive_id, token)
                log.info("初期化: %s... → %s", drive_id[:20], token)
            except Exception as e:
                log.error("初期化失敗: %s... %s", drive_id[:20], e)
        conn.close()
        return

    # 対象ドライブ
    if args.drive:
        target_drives = [args.drive] if args.drive in whitelist else []
        if not target_drives:
            log.error("指定ドライブ %s はホワイトリストにありません", args.drive)
            conn.close()
            return
    else:
        target_drives = whitelist

    # 遅延ロード: 変更があった時だけモデルを構築
    model = None
    splitter = None

    def _ensure_model():
        nonlocal model, splitter
        if model is None:
            model, splitter = _get_model_and_splitter()
        return model, splitter

    # 先に再試行キューを処理
    disk_full = False
    retry_recovered, retry_still_failed, retry_errors, disk_full = _retry_failed_files(
        service, conn, _ensure_model
    )

    if disk_full:
        log.error("再試行中にディスク満杯を検出、処理を中断")
        _save_sync_result(conn, {
            "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "aborted": True,
            "reason": "disk_full_during_retry",
            "retry_recovered": len(retry_recovered),
            "retry_still_failed": len(retry_still_failed),
        })
        conn.close()
        sys.exit(2)

    # トークンを 1 クエリでまとめて取得
    tokens = _load_tokens(conn, target_drives)

    all_added, all_updated, all_deleted = [], [], []
    total_errors = total_skipped = 0
    drives_with_changes = drives_checked = 0
    new_failed_files = []

    try:
        for drive_id in target_drives:
            token = tokens.get(drive_id)
            if token is None:
                # 未初期化: 今のトークンを取って保存するだけ
                try:
                    new_token = get_changes_start_token(service, drive_id=drive_id)
                    _save_token(conn, drive_id, new_token)
                    log.info("%s...: トークン初期化", drive_id[:16])
                except Exception as e:
                    log.warning("%s...: トークン取得失敗 %s", drive_id[:16], e)
                    total_errors += 1
                continue

            try:
                changes, new_token = list_changes(service, token, drive_id=drive_id)
            except Exception as e:
                log.warning("%s...: Changes取得失敗 %s", drive_id[:16], e)
                total_errors += 1
                continue

            drives_checked += 1

            if not changes:
                if new_token != token:
                    _save_token(conn, drive_id, new_token)
                continue

            drives_with_changes += 1
            log.info("%s...: %d件の変更", drive_id[:16], len(changes))

            _ensure_model()

            added, updated, deleted, errors, skipped, failed_ids = _process_drive_changes(
                service, conn, model, splitter, drive_id, changes
            )
            all_added.extend(added)
            all_updated.extend(updated)
            all_deleted.extend(deleted)
            total_errors += errors
            total_skipped += skipped
            new_failed_files.extend(failed_ids)

            # トークン保存: 失敗ファイルも retry queue に積まれているので安全に前進可能
            _save_token(conn, drive_id, new_token)

    except DiskFullHalt as e:
        log.error("ディスク満杯のため処理中断: %s", e)
        disk_full = True

    # retry queue の更新: 既存の still_failed + 今回の新 failed
    combined_failed = list(retry_still_failed) + list(new_failed_files)
    _save_failed_files(conn, combined_failed)

    # dead_files の現在数 (sync_history / 通知用)
    dead_count = len(_load_dead_files(conn))

    _save_sync_result(conn, {
        "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "added": all_added,
        "updated": all_updated,
        "deleted": all_deleted,
        "skipped": total_skipped,
        "errors": total_errors + retry_errors,
        "drives_checked": drives_checked,
        "drives_with_changes": drives_with_changes,
        "retry_recovered": len(retry_recovered),
        "retry_pending": len(combined_failed),
        "dead_files_total": dead_count,
        "disk_full": disk_full,
    })

    # issue #7: retry_pending しきい値超えで Telegram 通知
    _maybe_notify_retry_pending(conn, len(combined_failed), dead_count)

    total_changes = len(all_added) + len(all_updated) + len(all_deleted)
    if disk_full:
        log.error(
            "完了（中断）: 追加=%d 更新=%d 削除=%d エラー=%d 未処理=%d",
            len(all_added), len(all_updated), len(all_deleted),
            total_errors + retry_errors, len(combined_failed),
        )
        conn.close()
        sys.exit(2)
    elif total_changes == 0 and not retry_recovered:
        log.info(
            "変更なし (%d/%d drives) retry_pending=%d",
            drives_checked, len(target_drives), len(combined_failed),
        )
    else:
        log.info(
            "完了: 追加=%d 更新=%d 削除=%d スキップ=%d エラー=%d retry復旧=%d retry残=%d (%d/%d drives)",
            len(all_added), len(all_updated), len(all_deleted),
            total_skipped, total_errors + retry_errors,
            len(retry_recovered), len(combined_failed),
            drives_with_changes, drives_checked,
        )

    conn.close()


def main():
    parser = argparse.ArgumentParser(description="共有ドライブローテーション型差分同期")
    parser.add_argument("--init", action="store_true",
                        help="全ドライブの変更追跡トークンを初期化する")
    parser.add_argument("--db", type=int, default=None, metavar="N",
                        help="DB番号 (例: --db 3 → gridworldrag_3)")
    parser.add_argument("--drive", type=str, default=None, metavar="ID",
                        help="特定ドライブIDのみ処理")
    args = parser.parse_args()

    if args.db is not None:
        import src.config as _cfg
        _cfg.DB_NAME = f"gridworldrag_{args.db}"
        import src.db as _db
        _db.DB_NAME = f"gridworldrag_{args.db}"
        _setup_logging()
        log.info("DB: gridworldrag_%d", args.db)

    _acquire_lock()
    try:
        _run(args)
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
