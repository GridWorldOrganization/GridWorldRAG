"""PostgreSQL + pgvector データベース操作。"""

import json
import re
import sys

import psycopg2
from pgvector.psycopg2 import register_vector

from src.config import DB_NAME, DB_USER, DB_HOST, DB_PORT


def _escape_like_literal(value):
    """PostgreSQL LIKE のメタ文字 (\\ % _) をエスケープする。

    Drive file ID は base64url で '_' を含みうるため、生の値を LIKE パターンに
    埋め込むと '_' がワイルドカードとして解釈されて誤爆する。必ずこれを通すこと。
    呼び出し側は LIKE ... ESCAPE '\\' を指定すること。
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def connect(db_name=None):
    """PostgreSQL に接続する。db_name を指定すると config の DB_NAME を上書きする。"""
    try:
        conn = psycopg2.connect(
            dbname=db_name or DB_NAME,
            user=DB_USER,
            host=DB_HOST,
            port=DB_PORT,
        )
        register_vector(conn)
        return conn
    except psycopg2.OperationalError as e:
        print(f"エラー: PostgreSQL に接続できません: {e}")
        print("PostgreSQL が起動しているか確認してください:")
        print("  brew services start postgresql@17")
        sys.exit(1)


def insert_chunks(conn, chunks_data, commit=True):
    """チャンクデータを DB に一括挿入する（UPSERT）。

    commit=False の場合はトランザクションを閉じずに返す（呼び出し側で commit/rollback を管理）。
    """
    cur = conn.cursor()
    try:
        for chunk in chunks_data:
            # PostgreSQL は NUL (0x00) を含む文字列を受け付けない
            title = (chunk["title"] or "").replace("\x00", "")
            content = (chunk["content"] or "").replace("\x00", "")

            cur.execute(
                """
                INSERT INTO documents
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
                """,
                (
                    chunk["drive_file_id"],
                    title,
                    content,
                    chunk["chunk_index"],
                    chunk["owner"],
                    chunk["source_url"],
                    chunk["file_type"],
                    chunk["drive_modified_at"],
                    chunk["embedding"],
                    chunk.get("sheet_gid"),
                    chunk.get("sheet_name"),
                    json.dumps(chunk.get("permissions"), ensure_ascii=False) if chunk.get("permissions") else None,
                    chunk.get("partial_content", False),
                    chunk.get("folder_path", ""),
                ),
            )
        if commit:
            conn.commit()
    except Exception:
        if commit:
            conn.rollback()
        raise
    finally:
        cur.close()


def delete_by_file_id(conn, drive_file_id_prefix, commit=True):
    """指定した drive_file_id プレフィックスに一致するチャンクを削除する。

    Drive file ID は '_' を含みうるため LIKE メタ文字をエスケープする。
    commit=False の場合は呼び出し側でトランザクションを管理する。
    """
    cur = conn.cursor()
    try:
        pattern = _escape_like_literal(drive_file_id_prefix) + "%"
        cur.execute(
            r"DELETE FROM documents WHERE drive_file_id LIKE %s ESCAPE '\'",
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
    finally:
        cur.close()


def upsert_file_chunks(conn, file_id, chunks):
    """1ファイル分のチャンクをアトミックに置換する。

    既存チャンクを削除し新チャンクを挿入する操作を単一トランザクションで実行する。
    途中で失敗した場合は rollback し、DB 状態は変更されない。

    Returns:
        "updated": 既存チャンクを上書きした
        "added":   新規ファイルを追加した
    """
    # 既存チェック（読み取りのみ、commit 不要）
    cur = conn.cursor()
    try:
        pattern = _escape_like_literal(file_id) + "%"
        cur.execute(
            r"SELECT 1 FROM documents WHERE drive_file_id LIKE %s ESCAPE '\' LIMIT 1",
            (pattern,),
        )
        existed = cur.fetchone() is not None
    finally:
        cur.close()

    try:
        delete_by_file_id(conn, file_id, commit=False)
        insert_chunks(conn, chunks, commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return "updated" if existed else "added"


def file_exists(conn, file_id):
    """file_id のチャンクが DB に 1 件でも存在するかを返す。"""
    cur = conn.cursor()
    try:
        pattern = _escape_like_literal(file_id) + "%"
        cur.execute(
            r"SELECT 1 FROM documents WHERE drive_file_id LIKE %s ESCAPE '\' LIMIT 1",
            (pattern,),
        )
        return cur.fetchone() is not None
    finally:
        cur.close()


def extract_file_id_from_url(url):
    """Google Workspace の URL から FILE_ID を抽出する。

    対応 URL:
      - https://docs.google.com/spreadsheets/d/{ID}/edit...
      - https://docs.google.com/document/d/{ID}/edit...
      - https://docs.google.com/presentation/d/{ID}/edit...
      - https://drive.google.com/file/d/{ID}/view...
      - https://drive.google.com/open?id={ID}
      - https://drive.google.com/drive/folders/{ID}
    """
    patterns = [
        r"/d/([a-zA-Z0-9_-]+)",         # /d/{ID} パターン
        r"/folders/([a-zA-Z0-9_-]+)",    # /folders/{ID}
        r"[?&]id=([a-zA-Z0-9_-]+)",     # ?id={ID}
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def extract_gid_from_url(url):
    """スプレッドシート URL から gid（シートID）を抽出する。

    対応 URL:
      - ...edit?gid=660932359#gid=660932359
      - ...edit#gid=660932359
      - ...edit?gid=0
    """
    match = re.search(r'[?&#]gid=(\d+)', url)
    return match.group(1) if match else None


def lookup_by_url(conn, url):
    """Google Workspace の URL から DB 内の全チャンクを取得する。

    URL から FILE_ID を抽出し、該当ファイルの全チャンクを返す。
    スプレッドシートで gid が指定されている場合、そのシートのチャンクを優先表示する。

    Returns:
        dict: {
            "file_id": str,
            "title": str,
            "owner": str,
            "source_url": str,
            "file_type": str,
            "modified_at": str,
            "target_sheet": {"gid": str, "name": str} or None,
            "chunks": [{"index": int, "content": str,
                        "sheet_gid": str|None, "sheet_name": str|None}, ...],
            "full_text": str,
        }
        見つからない場合は None。
    """
    file_id = extract_file_id_from_url(url)
    if not file_id:
        return None

    target_gid = extract_gid_from_url(url)

    cur = conn.cursor()
    try:
        # file_id で始まる全チャンクを取得（_chunk_ と _sheet_ の両方に対応）
        # LIKE メタ文字をエスケープしてから '_%' を付けて「literal underscore + anything」を表す
        pattern = _escape_like_literal(file_id) + r"\_%"
        cur.execute(
            r"""
            SELECT title, content, chunk_index, owner, source_url,
                   file_type, drive_modified_at, sheet_gid, sheet_name
            FROM documents
            WHERE drive_file_id LIKE %s ESCAPE '\'
            ORDER BY
                CASE WHEN %s IS NOT NULL AND sheet_gid = %s THEN 0 ELSE 1 END,
                sheet_gid NULLS FIRST,
                chunk_index
            """,
            (pattern, target_gid, target_gid),
        )
        rows = cur.fetchall()
    finally:
        cur.close()

    if not rows:
        return None

    first = rows[0]
    chunks = [
        {
            "index": r[2],
            "content": r[1],
            "sheet_gid": r[7],
            "sheet_name": r[8],
        }
        for r in rows
    ]
    full_text = "\n".join(r[1] for r in rows)

    # ターゲットシートの情報
    target_sheet = None
    if target_gid:
        for r in rows:
            if r[7] == target_gid:
                target_sheet = {"gid": r[7], "name": r[8]}
                break

    return {
        "file_id": file_id,
        "title": first[0],
        "owner": first[3],
        "source_url": first[4],
        "file_type": first[5],
        "modified_at": str(first[6]) if first[6] else None,
        "target_sheet": target_sheet,
        "chunks": chunks,
        "full_text": full_text,
    }


def search_similar(conn, embedding, n_results=5, owner=None, since=None):
    """ベクトル類似度検索。オプションでメタデータフィルタ付き。"""
    import numpy as np
    if isinstance(embedding, list):
        embedding = np.array(embedding, dtype=np.float32)

    conditions = []
    filter_params = []

    if owner:
        conditions.append("owner = %s")
        filter_params.append(owner)
    if since:
        conditions.append("drive_modified_at > %s")
        filter_params.append(since)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    # パラメータ順: distance計算用embedding, フィルタ値..., ソート用embedding, LIMIT
    params = [embedding] + filter_params + [embedding, n_results]

    cur = conn.cursor()
    try:
        cur.execute(
            f"""
            SELECT id, title, content, owner, source_url, file_type,
                   drive_modified_at, embedding <=> %s AS distance,
                   sheet_gid, sheet_name
            FROM documents
            {where}
            ORDER BY embedding <=> %s
            LIMIT %s
            """,
            tuple(params),
        )
        results = cur.fetchall()
        return results
    finally:
        cur.close()
