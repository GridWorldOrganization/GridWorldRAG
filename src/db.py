"""PostgreSQL + pgvector データベース操作。"""

import psycopg2
from pgvector.psycopg2 import register_vector

from src.config import DB_NAME, DB_USER, DB_HOST, DB_PORT


def connect():
    """PostgreSQL に接続する。"""
    conn = psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        host=DB_HOST,
        port=DB_PORT,
    )
    register_vector(conn)
    return conn


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


def search_similar(conn, embedding, n_results=5, owner=None, since=None):
    """ベクトル類似度検索。オプションでメタデータフィルタ付き。"""
    conditions = []
    params = [embedding]

    if owner:
        conditions.append("owner = %s")
        params.append(owner)
    if since:
        conditions.append("drive_modified_at > %s")
        params.append(since)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.append(n_results)

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
        (*params[:1], *params),
    )
    results = cur.fetchall()
    cur.close()
    return results
