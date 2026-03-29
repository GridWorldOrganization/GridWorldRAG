#!/usr/bin/env python3
"""
sync.py - Google Drive の差分変更を検知して DB を差分更新する。

使い方:
    python sync.py           # 差分同期
    python sync.py --init    # 変更追跡トークンを初期化（ファイル処理なし）
    python sync.py --db N    # DB番号指定 (例: --db 1 → gridworldrag_1)
"""

import argparse
import sys
import threading
import time

from langchain_text_splitters import RecursiveCharacterTextSplitter
from sentence_transformers import SentenceTransformer

from src.config import (
    EMBEDDING_MODEL, CHUNK_SIZE, CHUNK_OVERLAP,
    DRIVE_DOWNLOAD_TIMEOUT_SEC,
    load_shared_drives_whitelist,
)
from src.drive_client import (
    authenticate,
    get_changes_start_token,
    list_changes,
    extract_text,
    extract_spreadsheet_sheets,
    _download_with_sigalrm,
    _DownloadTimeoutError,
    SKIP_MIME_TYPES,
    resolve_folder_path_api,
)
from src.db import connect, insert_chunks, delete_by_file_id
from src.indexer import make_chunk_entry


# ---------------------------------------------------------------------------
# sync_state テーブル
# ---------------------------------------------------------------------------

_SYNC_STATE_DDL = """
CREATE TABLE IF NOT EXISTS sync_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT NOW()
);
"""
_TOKEN_KEY = "changes_page_token"


def _ensure_sync_state_table(conn):
    cur = conn.cursor()
    cur.execute(_SYNC_STATE_DDL)
    conn.commit()
    cur.close()


def _load_token(conn):
    cur = conn.cursor()
    cur.execute("SELECT value FROM sync_state WHERE key = %s", (_TOKEN_KEY,))
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


def _save_token(conn, token):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sync_state (key, value, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (key) DO UPDATE SET
            value = EXCLUDED.value,
            updated_at = NOW()
        """,
        (_TOKEN_KEY, token),
    )
    conn.commit()
    cur.close()


_RESULT_KEY = "last_sync_result"


def _save_sync_result(conn, result_dict):
    import json as _json
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sync_state (key, value, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (key) DO UPDATE SET
            value = EXCLUDED.value,
            updated_at = NOW()
        """,
        (_RESULT_KEY, _json.dumps(result_dict, ensure_ascii=False)),
    )
    conn.commit()
    cur.close()


# ---------------------------------------------------------------------------
# ドライブ所属チェック
# ---------------------------------------------------------------------------

def _get_file_drive_id(service, file_id):
    """Drive API でファイルの driveId を取得する（共有ドライブ外は None）。"""
    try:
        result = service.files().get(
            fileId=file_id,
            fields="driveId",
            supportsAllDrives=True,
        ).execute()
        return result.get("driveId")
    except Exception as e:
        print(f"  警告: driveId 取得失敗 [{file_id}]: {e}", flush=True)
        return None


# ---------------------------------------------------------------------------
# 1ファイルの処理
# ---------------------------------------------------------------------------

def _process_changed_file(file_info, service, model, splitter, whitelist):
    """変更ファイルを処理してチャンクリストを返す。

    Returns:
        (chunks, status): status は "ok" | "skip" | "error"
    """
    file_id = file_info["id"]
    file_name = file_info.get("name", "?")
    mime_type = file_info.get("mimeType", "")

    # スキップ対象 MIME
    if mime_type in SKIP_MIME_TYPES:
        return [], "skip"

    # ホワイトリストチェック
    drive_id = _get_file_drive_id(service, file_id)
    if drive_id is not None and drive_id not in whitelist:
        return [], "skip"

    # スプレッドシート
    if mime_type == "application/vnd.google-apps.spreadsheet":
        try:
            sheets = _download_with_sigalrm(
                lambda: extract_spreadsheet_sheets(file_id),
                DRIVE_DOWNLOAD_TIMEOUT_SEC,
            )
        except _DownloadTimeoutError:
            print(f"  警告: スプレッドシートタイムアウト [{file_name}]", flush=True)
            return [], "error"

        if not sheets:
            return [], "skip"

        chunks = []
        for sheet in sheets:
            is_partial = sheet.get("failed", False)
            content = sheet.get("content")
            sheet_text = (f"[シート: {sheet['name']}]\n{content}"
                          if content else f"[シート: {sheet['name']}]")
            text_chunks = splitter.split_text(sheet_text) or [sheet_text]
            try:
                embeddings = model.encode(text_chunks)
            except Exception as e:
                print(f"  警告: embed 失敗 [{file_name}/{sheet['name']}]: {e}", flush=True)
                continue
            for ci, (chunk_text, emb) in enumerate(zip(text_chunks, embeddings)):
                chunks.append(make_chunk_entry(
                    file_info, chunk_text, emb, ci,
                    sheet_gid=sheet["gid"], sheet_name=sheet["name"],
                    partial_content=is_partial,
                ))
        return (chunks, "ok") if chunks else ([], "skip")

    # その他（Docs, PDF, テキスト, 画像, 動画, 音声, フォルダ）
    text, is_partial = extract_text(service, file_info)
    if not text or not text.strip():
        return [], "skip"

    text_chunks = splitter.split_text(text)
    if not text_chunks:
        return [], "skip"

    try:
        embeddings = model.encode(text_chunks)
    except Exception as e:
        print(f"  警告: embed 失敗 [{file_name}]: {e}", flush=True)
        return [], "error"

    chunks = [
        make_chunk_entry(file_info, chunk_text, emb, ci, partial_content=is_partial)
        for ci, (chunk_text, emb) in enumerate(zip(text_chunks, embeddings))
    ]
    return chunks, "ok"


# ---------------------------------------------------------------------------
# 差分処理メインループ
# ---------------------------------------------------------------------------

def _run_sync(service, conn, model, splitter, whitelist):
    token = _load_token(conn)
    if token is None:
        print(f"[{time.strftime('%H:%M:%S')}] トークン未初期化。--init で初期化してください。")
        sys.exit(1)

    print(f"[{time.strftime('%H:%M:%S')}] 変更取得中...", flush=True)
    changes, new_token = list_changes(service, token)

    if not changes:
        print(f"[{time.strftime('%H:%M:%S')}] 変更なし", flush=True)
        _save_token(conn, new_token)
        return

    print(f"[{time.strftime('%H:%M:%S')}] {len(changes)} 件の変更を検出", flush=True)

    added = updated = deleted = skipped = errors = 0
    added_files = []
    updated_files = []
    deleted_files = []
    _folder_cache = {}  # フォルダパス解決キャッシュ

    for change in changes:
        file_id = change.get("fileId")
        is_removed = change.get("removed", False)
        file_info = change.get("file")

        # 削除・ゴミ箱
        if is_removed or (file_info and file_info.get("trashed", False)):
            n = delete_by_file_id(conn, file_id)
            if n > 0:
                fname = file_info.get("name", file_id) if file_info else file_id
                furl = (file_info.get("webViewLink", "") if file_info else "")
                print(f"[{time.strftime('%H:%M:%S')}] 削除: {fname}", flush=True)
                deleted += 1
                deleted_files.append({"name": fname, "url": furl, "id": file_id})
            continue

        if file_info is None:
            skipped += 1
            continue

        fname = file_info.get("name", "?")
        furl = file_info.get("webViewLink", "")
        file_info["folder_path"] = resolve_folder_path_api(service, file_info, _folder_cache)

        # threading タイムアウト付きでファイル処理
        _result = [None]
        _error = [None]

        def _run():
            try:
                _result[0] = _process_changed_file(
                    file_info, service, model, splitter, whitelist
                )
            except Exception as e:
                _error[0] = e

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=DRIVE_DOWNLOAD_TIMEOUT_SEC)

        if t.is_alive():
            print(f"[{time.strftime('%H:%M:%S')}] タイムアウト: {fname}", flush=True)
            errors += 1
            continue

        if _error[0] is not None:
            print(f"[{time.strftime('%H:%M:%S')}] エラー: {fname}  ({_error[0]})", flush=True)
            errors += 1
            continue

        chunks, status = _result[0]

        if status in ("skip", "error"):
            skipped += 1 if status == "skip" else 0
            errors += 1 if status == "error" else 0
            continue

        # 既存チャンクを削除して再挿入（チャンク数変化に対応）
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM documents WHERE drive_file_id LIKE %s LIMIT 1",
            (f"{file_id}%",),
        )
        exists = cur.fetchone() is not None
        cur.close()

        delete_by_file_id(conn, file_id)
        insert_chunks(conn, chunks)

        label = "更新" if exists else "追加"
        print(f"[{time.strftime('%H:%M:%S')}] {label}: {fname}", flush=True)
        if exists:
            updated += 1
            updated_files.append({"name": fname, "url": furl, "id": file_id})
        else:
            added += 1
            added_files.append({"name": fname, "url": furl, "id": file_id})

    import json as _json
    _save_sync_result(conn, {
        "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "added": added_files,
        "updated": updated_files,
        "deleted": deleted_files,
        "skipped": skipped,
        "errors": errors,
    })
    _save_token(conn, new_token)
    print(
        f"[{time.strftime('%H:%M:%S')}] 完了: "
        f"追加={added} 更新={updated} 削除={deleted} "
        f"スキップ={skipped} エラー={errors}",
        flush=True,
    )


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Google Drive 差分同期")
    parser.add_argument("--init", action="store_true",
                        help="変更追跡トークンを初期化する（ファイル処理なし）")
    parser.add_argument("--db", type=int, default=None, metavar="N",
                        help="使用するDB番号 (例: --db 1 → gridworldrag_1)")
    args = parser.parse_args()

    if args.db is not None:
        import os
        os.environ["GRIDWORLDRAG_DB_INDEX"] = str(args.db)
        import src.config as _cfg
        _cfg.DB_NAME = f"gridworldrag_{args.db}"
        import src.db as _db
        _db.DB_NAME = f"gridworldrag_{args.db}"
        print(f"DB: gridworldrag_{args.db} を使用", flush=True)

    print(f"[{time.strftime('%H:%M:%S')}] Google Drive 認証中...", end="", flush=True)
    service = authenticate()
    print(" 完了", flush=True)

    conn = connect()
    _ensure_sync_state_table(conn)

    if args.init:
        token = get_changes_start_token(service)
        _save_token(conn, token)
        print(f"[{time.strftime('%H:%M:%S')}] トークン初期化完了: {token}", flush=True)
        conn.close()
        return

    print(f"[{time.strftime('%H:%M:%S')}] モデルロード中...", end="", flush=True)
    model = SentenceTransformer(EMBEDDING_MODEL)
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP,
    )
    print(" 完了", flush=True)

    whitelist = load_shared_drives_whitelist()
    _run_sync(service, conn, model, splitter, whitelist)
    conn.close()


if __name__ == "__main__":
    main()
