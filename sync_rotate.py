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
import os
import sys
import time
from pathlib import Path

from src.config import (
    EMBEDDING_MODEL, CHUNK_SIZE, CHUNK_OVERLAP,
    load_shared_drives_whitelist,
)
from src.drive_client import (
    authenticate,
    get_changes_start_token,
    list_changes,
    extract_text,
    extract_spreadsheet_sheets,
    resolve_folder_path_api,
    SKIP_MIME_TYPES,
)
from src.db import connect, upsert_file_chunks, delete_by_file_id
from src.indexer import make_chunk_entry

LOCK_FILE = Path("/tmp/gridworldrag_rotate.lock")
_STALE_LOCK_SEC = 1200  # 20分以上古いロックは stale 扱い


def _acquire_lock():
    if LOCK_FILE.exists():
        try:
            age = time.time() - LOCK_FILE.stat().st_mtime
        except FileNotFoundError:
            age = _STALE_LOCK_SEC + 1
        if age <= _STALE_LOCK_SEC:
            print(f"[{time.strftime('%H:%M:%S')}] 前回実行中 (age={int(age)}s) スキップ", flush=True)
            sys.exit(0)
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
"""
_TOKEN_PREFIX = "rotate_token_"
_RESULT_KEY = "last_sync_result"


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
                print(f"  警告: embed失敗 [{file_info.get('name','?')}/{sheet['name']}]: {e}", flush=True)
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
    """1ドライブの Changes API 結果を処理する。"""
    added, updated, deleted = [], [], []
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
                print(f"  警告: 削除失敗 [{file_id}]: {e}", flush=True)
                errors += 1
                continue
            if n > 0:
                fname = (file_info or {}).get("name", file_id)
                furl = (file_info or {}).get("webViewLink", "")
                print(f"  削除: {fname}", flush=True)
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
            print(f"  エラー: {fname} ({e})", flush=True)
            errors += 1
            continue

        if not chunks:
            skipped += 1
            continue

        try:
            status = upsert_file_chunks(conn, file_id, chunks)
        except Exception as e:
            print(f"  警告: DB書込失敗 [{fname}]: {e}", flush=True)
            errors += 1
            continue

        entry = {"name": fname, "url": furl, "id": file_id}
        if status == "updated":
            print(f"  更新: {fname}", flush=True)
            updated.append(entry)
        else:
            print(f"  追加: {fname}", flush=True)
            added.append(entry)

    return added, updated, deleted, errors, skipped


# ---------------------------------------------------------------------------
# メインループ
# ---------------------------------------------------------------------------

def _run(args):
    whitelist = load_shared_drives_whitelist()
    if not whitelist:
        print("ホワイトリストが空です", flush=True)
        return

    print(f"[{time.strftime('%H:%M:%S')}] Drive認証中...", end="", flush=True)
    service = authenticate()
    print(" 完了", flush=True)

    conn = connect()
    _ensure_sync_state_table(conn)

    # --init: 全ドライブのトークンを取得して保存
    if args.init:
        for drive_id in whitelist:
            try:
                token = get_changes_start_token(service, drive_id=drive_id)
                _save_token(conn, drive_id, token)
                print(f"  初期化: {drive_id[:20]}... → {token}", flush=True)
            except Exception as e:
                print(f"  エラー: {drive_id[:20]}... {e}", flush=True)
        conn.close()
        return

    # 対象ドライブ
    if args.drive:
        target_drives = [args.drive] if args.drive in whitelist else []
        if not target_drives:
            print(f"指定ドライブ {args.drive} はホワイトリストにありません", flush=True)
            conn.close()
            return
    else:
        target_drives = whitelist

    # トークンを 1 クエリでまとめて取得
    tokens = _load_tokens(conn, target_drives)

    # 遅延ロード: 変更があった時だけモデルを構築
    model = None
    splitter = None

    all_added, all_updated, all_deleted = [], [], []
    total_errors = total_skipped = 0
    drives_with_changes = drives_checked = 0

    for drive_id in target_drives:
        token = tokens.get(drive_id)
        if token is None:
            # 未初期化: 今のトークンを取って保存するだけ（次回から効く）
            try:
                new_token = get_changes_start_token(service, drive_id=drive_id)
                _save_token(conn, drive_id, new_token)
                print(f"[{time.strftime('%H:%M:%S')}] {drive_id[:16]}...: トークン初期化", flush=True)
            except Exception as e:
                print(f"[{time.strftime('%H:%M:%S')}] {drive_id[:16]}...: トークン取得失敗 {e}", flush=True)
                total_errors += 1
            continue

        try:
            changes, new_token = list_changes(service, token, drive_id=drive_id)
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] {drive_id[:16]}...: Changes取得失敗 {e}", flush=True)
            total_errors += 1
            continue

        drives_checked += 1

        if not changes:
            # 変更なし: トークンがローテーションした時だけ更新（fast path の fsync を削減）
            if new_token != token:
                _save_token(conn, drive_id, new_token)
            continue

        drives_with_changes += 1
        print(f"[{time.strftime('%H:%M:%S')}] {drive_id[:16]}...: {len(changes)}件の変更", flush=True)

        # モデル遅延ロード
        if model is None:
            print(f"[{time.strftime('%H:%M:%S')}] モデルロード中...", end="", flush=True)
            from sentence_transformers import SentenceTransformer
            from langchain_text_splitters import RecursiveCharacterTextSplitter
            model = SentenceTransformer(EMBEDDING_MODEL)
            splitter = RecursiveCharacterTextSplitter(
                chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP,
            )
            print(" 完了", flush=True)

        added, updated, deleted, errors, skipped = _process_drive_changes(
            service, conn, model, splitter, drive_id, changes
        )
        all_added.extend(added)
        all_updated.extend(updated)
        all_deleted.extend(deleted)
        total_errors += errors
        total_skipped += skipped

        # トークン保存は処理成功後（エラーがあっても前進させる実装にするなら errors 判定を外す）
        # WHY: ここで新トークンを保存しないと次回同じ変更を再フェッチすることになり、
        # 変更ゼロ時の fast path で余計な処理が発生する。エラーファイルは再取得不能だが、
        # 手動で --init で再初期化できる。
        _save_token(conn, drive_id, new_token)

    _save_sync_result(conn, {
        "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "added": all_added,
        "updated": all_updated,
        "deleted": all_deleted,
        "skipped": total_skipped,
        "errors": total_errors,
        "drives_checked": drives_checked,
        "drives_with_changes": drives_with_changes,
    })

    total_changes = len(all_added) + len(all_updated) + len(all_deleted)
    if total_changes == 0:
        print(
            f"[{time.strftime('%H:%M:%S')}] 変更なし "
            f"({drives_checked}/{len(target_drives)} drives)",
            flush=True,
        )
    else:
        print(
            f"[{time.strftime('%H:%M:%S')}] 完了: "
            f"追加={len(all_added)} 更新={len(all_updated)} 削除={len(all_deleted)} "
            f"スキップ={total_skipped} エラー={total_errors} "
            f"({drives_with_changes}/{drives_checked} drives)",
            flush=True,
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
        print(f"DB: gridworldrag_{args.db}", flush=True)

    _acquire_lock()
    try:
        _run(args)
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
