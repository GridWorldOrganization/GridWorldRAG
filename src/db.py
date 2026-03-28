"""PostgreSQL + pgvector データベース操作。"""

import re
import sys

import psycopg2
from pgvector.psycopg2 import register_vector

from src.config import DB_NAME, DB_USER, DB_HOST, DB_PORT


def connect():
    """PostgreSQL に接続する。"""
    try:
        conn = psycopg2.connect(
            dbname=DB_NAME,
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


def insert_chunks(conn, chunks_data):
    """チャンクデータを DB に一括挿入する（UPSERT）。"""
    cur = conn.cursor()
    for chunk in chunks_data:
        cur.execute(
            """
            INSERT INTO documents
                (drive_file_id, title, content, chunk_index, owner,
                 source_url, file_type, drive_modified_at, embedding)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (drive_file_id) DO UPDATE SET
                title = EXCLUDED.title,
                content = EXCLUDED.content,
                chunk_index = EXCLUDED.chunk_index,
                owner = EXCLUDED.owner,
                source_url = EXCLUDED.source_url,
                file_type = EXCLUDED.file_type,
                drive_modified_at = EXCLUDED.drive_modified_at,
                embedding = EXCLUDED.embedding
            """,
            (
                chunk["drive_file_id"],
                chunk["title"],
                chunk["content"],
                chunk["chunk_index"],
                chunk["owner"],
                chunk["source_url"],
                chunk["file_type"],
                chunk["drive_modified_at"],
                chunk["embedding"],
            ),
        )
    conn.commit()
    cur.close()


def delete_by_file_id(conn, drive_file_id_prefix):
    """指定した drive_file_id プレフィックスに一致するチャンクを削除する。"""
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM documents WHERE drive_file_id LIKE %s",
        (f"{drive_file_id_prefix}%",),
    )
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    return deleted


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


def lookup_by_url(conn, url):
    """Google Workspace の URL から DB 内の全チャンクを取得する。

    URL から FILE_ID を抽出し、drive_file_id が "{FILE_ID}_chunk_%" に一致する
    全チャンクをチャンク順に返す。

    Returns:
        dict: {
            "file_id": str,
            "title": str,
            "owner": str,
            "source_url": str,
            "file_type": str,
            "modified_at": str,
            "chunks": [{"index": int, "content": str}, ...],
            "full_text": str,  # 全チャンクを結合したテキスト
        }
        見つからない場合は None。
    """
    file_id = extract_file_id_from_url(url)
    if not file_id:
        return None

    cur = conn.cursor()
    cur.execute(
        """
        SELECT title, content, chunk_index, owner, source_url,
               file_type, drive_modified_at
        FROM documents
        WHERE drive_file_id LIKE %s
        ORDER BY chunk_index
        """,
        (f"{file_id}_chunk_%",),
    )
    rows = cur.fetchall()
    cur.close()

    if not rows:
        return None

    first = rows[0]
    chunks = [{"index": r[2], "content": r[1]} for r in rows]
    full_text = "\n".join(r[1] for r in rows)

    return {
        "file_id": file_id,
        "title": first[0],
        "owner": first[3],
        "source_url": first[4],
        "file_type": first[5],
        "modified_at": str(first[6]) if first[6] else None,
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
    cur.execute(
        f"""
        SELECT id, title, content, owner, source_url, file_type,
               drive_modified_at, embedding <=> %s AS distance
        FROM documents
        {where}
        ORDER BY embedding <=> %s
        LIMIT %s
        """,
        tuple(params),
    )
    results = cur.fetchall()
    cur.close()
    return results
