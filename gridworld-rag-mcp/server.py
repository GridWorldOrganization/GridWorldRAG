"""
gridworld-rag-mcp - GridWorldRAG の MCP サーバー

Google Drive のインデックスDB（PostgreSQL + pgvector）に対して
セマンティック検索・URL検索・ファイル一覧を提供する。

Claude Code からの登録:
    claude mcp add gridworld-rag-mcp -- python gridworld-rag-mcp/server.py
"""

import re
import sys
import os

# プロジェクトルートを参照できるようにする
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# --db N オプションで接続先DBを切り替え（起動時のみ有効）
_db_name = None
for _i, _arg in enumerate(sys.argv[1:]):
    if _arg == "--db" and _i + 1 < len(sys.argv) - 1:
        _db_index = sys.argv[_i + 2]
        _db_name = f"gridworldrag_{_db_index}"
        os.environ["GRIDWORLDRAG_DB_INDEX"] = _db_index
        break

from mcp.server.fastmcp import FastMCP

from src.config import EMBEDDING_MODEL
from src.db import connect, search_similar, lookup_by_url, extract_file_id_from_url

mcp = FastMCP("gridworld-rag-mcp")

# DB接続（サーバー起動時に1回だけ接続し、使い回す）
_conn = None


def _get_conn():
    global _conn
    if _conn is None or _conn.closed:
        _conn = connect(_db_name)
    else:
        try:
            _conn.cursor().execute("SELECT 1")
        except Exception:
            _conn = connect(_db_name)
    return _conn


# 埋め込みモデル（サーバー起動時にプリロード）
from sentence_transformers import SentenceTransformer
_model = SentenceTransformer(EMBEDDING_MODEL)


def _contains_url(text):
    """テキストに Google Drive/Docs の URL が含まれているか判定する。"""
    return bool(re.search(
        r"https?://(docs\.google\.com|drive\.google\.com)/",
        text
    ))


@mcp.tool()
def search(query: str, n_results: int = 5, owner: str = None) -> str:
    """Google Drive のインデックスDBをセマンティック検索する。

    自然言語のクエリで関連するドキュメントを検索する。
    クエリに Google Drive/Docs の URL が含まれている場合は、
    そのファイルの内容を直接取得する。

    Args:
        query: 検索クエリ（自然言語またはURL）
        n_results: 返す結果の数（デフォルト: 5）
        owner: オーナーのメールアドレスでフィルタ（省略可）
    """
    conn = _get_conn()

    # URL が含まれていたら lookup_by_url を先に試す
    if _contains_url(query):
        urls = re.findall(
            r"https?://(?:docs|drive)\.google\.com/\S+",
            query
        )
        results = []
        for url in urls:
            doc = lookup_by_url(conn, url)
            if doc:
                results.append(_format_lookup_result(doc))

        # URL以外のテキストでも検索
        non_url_query = re.sub(
            r"https?://(?:docs|drive)\.google\.com/\S+",
            "",
            query
        ).strip()

        if non_url_query:
            embedding = _model.encode(non_url_query)
            similar = search_similar(conn, embedding, n_results=n_results, owner=owner)
            results.extend([_format_search_result(r) for r in similar])

        if results:
            return "\n\n---\n\n".join(results)
        return "該当するドキュメントが見つかりませんでした。"

    # 通常のセマンティック検索
    embedding = _model.encode(query)
    results = search_similar(conn, embedding, n_results=n_results, owner=owner)

    if not results:
        return "該当するドキュメントが見つかりませんでした。"

    return "\n\n---\n\n".join([_format_search_result(r) for r in results])


@mcp.tool()
def lookup(url: str) -> str:
    """Google Drive/Docs の URL からファイルの内容を取得する。

    スプレッドシートの場合、URL に gid パラメータがあれば
    そのシートのチャンクを優先して返す。

    Args:
        url: Google Drive/Docs/Sheets/Slides の URL
    """
    conn = _get_conn()
    doc = lookup_by_url(conn, url)
    if not doc:
        return f"該当するドキュメントがDBに見つかりませんでした。\nURL: {url}"
    return _format_lookup_result(doc)


@mcp.tool()
def stats() -> str:
    """インデックスDBの統計情報を返す。

    総レコード数、ファイル数、ドライブ別の内訳等。
    """
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM documents")
    total_rows = cur.fetchone()[0]

    cur.execute("SELECT COUNT(DISTINCT title) FROM documents")
    total_files = cur.fetchone()[0]

    cur.execute("""
        SELECT file_type, COUNT(DISTINCT title)
        FROM documents
        GROUP BY file_type
        ORDER BY COUNT(DISTINCT title) DESC
        LIMIT 10
    """)
    type_counts = cur.fetchall()

    cur.execute("""
        SELECT owner, COUNT(DISTINCT title)
        FROM documents
        WHERE owner != ''
        GROUP BY owner
        ORDER BY COUNT(DISTINCT title) DESC
        LIMIT 10
    """)
    owner_counts = cur.fetchall()

    cur.close()

    lines = [
        f"## インデックスDB統計",
        f"- 総レコード数: {total_rows}",
        f"- ファイル数: {total_files}",
        "",
        "### ファイルタイプ別",
    ]
    for ft, cnt in type_counts:
        lines.append(f"- {ft}: {cnt}")

    lines.append("")
    lines.append("### オーナー別")
    for owner, cnt in owner_counts:
        lines.append(f"- {owner}: {cnt}")

    return "\n".join(lines)


def _format_search_result(row):
    """search_similar の結果行をフォーマットする。"""
    # row: (id, title, content, owner, source_url, file_type, modified_at, distance, sheet_gid, sheet_name)
    title = row[1]
    content = row[2]
    owner = row[3]
    url = row[4]
    file_type = row[5]
    distance = row[7]
    sheet_name = row[9] if len(row) > 9 else None

    lines = [f"### {title}"]
    if sheet_name:
        lines.append(f"シート: {sheet_name}")
    lines.append(f"タイプ: {file_type} | オーナー: {owner} | 類似度: {1 - distance:.3f}")
    if url:
        lines.append(f"URL: {url}")
    lines.append("")
    lines.append(content[:2000])
    return "\n".join(lines)


def _format_lookup_result(doc):
    """lookup_by_url の結果をフォーマットする。"""
    lines = [f"### {doc['title']}"]
    if doc.get("target_sheet"):
        lines.append(f"シート: {doc['target_sheet']['name']} (gid: {doc['target_sheet']['gid']})")
    lines.append(f"オーナー: {doc['owner']} | タイプ: {doc['file_type']}")
    if doc.get("modified_at"):
        lines.append(f"更新日: {doc['modified_at']}")
    if doc.get("source_url"):
        lines.append(f"URL: {doc['source_url']}")
    lines.append(f"チャンク数: {len(doc['chunks'])}")
    lines.append("")
    # 全文（長すぎる場合は切り詰め）
    full_text = doc["full_text"]
    if len(full_text) > 5000:
        lines.append(full_text[:5000])
        lines.append(f"\n... (残り {len(full_text) - 5000} 文字)")
    else:
        lines.append(full_text)
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
