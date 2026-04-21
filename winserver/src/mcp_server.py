"""MCP (Model Context Protocol) server for Claude Cowork / Desktop clients.

This exposes the WinServerRAG index over Streamable HTTP so a remote Claude
can search the user's Google Drive shared folders.

Auth: HTTP Basic Auth keyed on public.mcp_users (see src/mcp_auth.py).
Only drives with `search_enabled = TRUE` are ever searched — the admin
controls this via the Windows monitor's MCP tab.
"""
from __future__ import annotations

import base64
import logging
from typing import Optional

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount

from src import db
from src.config import EMBEDDING_MODEL, EMBEDDING_DEVICE
from src.mcp_auth import verify_password

log = logging.getLogger("mcp_server")

# ---------------------------------------------------------------------
# Embedding (lazy, per-process cache)
# ---------------------------------------------------------------------
_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        import torch
        dev = EMBEDDING_DEVICE
        if dev == "auto":
            dev = "cuda" if torch.cuda.is_available() else "cpu"
        _model = SentenceTransformer(EMBEDDING_MODEL, device=dev)
    return _model


def _embed_query(text: str):
    model = _get_model()
    return model.encode([text], show_progress_bar=False, convert_to_numpy=True,
                        normalize_embeddings=False)[0]


# ---------------------------------------------------------------------
# MCP server (FastMCP) — tools exposed to Claude
# ---------------------------------------------------------------------
mcp = FastMCP(
    name="WinServerRAG",
    instructions=(
        "Search the user's Google Drive shared folders that have been indexed "
        "by WinServerRAG. Use the `search` tool with a natural-language query "
        "to find relevant document chunks. Use `list_drives` to see which "
        "shared drives are currently in scope, and `lookup` to fetch the full "
        "content of a specific Google Drive URL."
    ),
    # Serve the Streamable HTTP transport at "/" of the inner app so that
    # once we mount it under "/mcp" in the main FastAPI, the endpoint is
    # just /mcp (not /mcp/mcp). Trailing slash is also accepted.
    streamable_http_path="/",
)


@mcp.tool()
def list_drives() -> dict:
    """List the shared drives currently in MCP search scope.

    Returns the drive_id, name, and chunk count for every drive the admin
    has enabled for Claude Cowork access. Empty if the admin hasn't enabled
    any drive yet.
    """
    conn = db.connect()
    try:
        fds = db.list_fds(conn)
    finally:
        conn.close()
    scoped = [
        {
            "drive_id":   f["drive_id"],
            "name":       f["name"],
            "file_count": f["file_count"],
            "chunk_count": f["chunk_count"],
            "last_build_at": str(f["last_build_at"]) if f["last_build_at"] else None,
        }
        for f in fds if f.get("search_enabled")
    ]
    return {"drives": scoped, "count": len(scoped)}


@mcp.tool()
def search(query: str, n_results: int = 10, owner: Optional[str] = None) -> dict:
    """Semantic search across all MCP-enabled shared drives.

    Args:
        query: natural-language query (Japanese or English).
        n_results: maximum number of chunks to return. 1..50. Default 10.
        owner: optional email-address filter — only returns chunks owned by
               that person.
    """
    n_results = max(1, min(50, int(n_results)))
    conn = db.connect()
    try:
        schemas = db.search_enabled_schemas(conn)
        if not schemas:
            return {
                "results": [],
                "note": ("No drives are in MCP scope. Ask the administrator "
                         "to enable at least one drive on the Windows monitor "
                         "MCP tab."),
            }
        emb = _embed_query(query)
        rows = db.search_across_schemas(conn, emb, schemas, n_results=n_results,
                                        owner=owner)
    finally:
        conn.close()
    drive_names = {d: n for d, n in schemas}
    results = [
        {
            "drive_id":    r["drive_id"],
            "drive_name":  drive_names.get(r["drive_id"], ""),
            "title":       r["title"],
            "content":     r["content"],
            "owner":       r["owner"],
            "source_url":  r["source_url"],
            "file_type":   r["file_type"],
            "modified_at": str(r["drive_modified_at"]) if r["drive_modified_at"] else None,
            "sheet_gid":   r["sheet_gid"],
            "sheet_name":  r["sheet_name"],
            "folder_path": r["folder_path"],
            "distance":    float(r["distance"]),
        }
        for r in rows
    ]
    return {"results": results, "count": len(results), "query": query}


@mcp.tool()
def lookup(url: str) -> dict:
    """Fetch the full indexed content for a specific Google Drive URL.

    Works on Docs / Sheets / Slides / Drive-file links. For spreadsheets with
    `gid` in the URL, the specified sheet is surfaced first in the chunk order.
    Respects the MCP search scope: lookup succeeds only if the file's drive
    is currently enabled for MCP search.
    """
    file_id = db.extract_file_id_from_url(url)
    if not file_id:
        return {"error": "could not extract file_id from URL"}
    target_gid = db.extract_gid_from_url(url)

    conn = db.connect()
    try:
        schemas = db.search_enabled_schemas(conn)
        if not schemas:
            return {"error": "no drives in MCP scope"}
        # Find which schema contains this file_id.
        with conn.cursor() as cur:
            pattern = db._file_id_like_pattern(file_id)
            for drive_id, drive_name in schemas:
                schema = db.schema_for_drive(drive_id)
                cur.execute(
                    f"""
                    SELECT title, content, chunk_index, owner, source_url,
                           file_type, drive_modified_at, sheet_gid, sheet_name,
                           folder_path
                    FROM {db._quote_ident(schema)}.documents
                    WHERE drive_file_id LIKE %s ESCAPE '\\'
                    ORDER BY
                        CASE WHEN %s IS NOT NULL AND sheet_gid = %s THEN 0 ELSE 1 END,
                        sheet_gid NULLS FIRST,
                        chunk_index
                    """,
                    (pattern, target_gid, target_gid),
                )
                rows = cur.fetchall()
                if rows:
                    chunks = [
                        {
                            "index":     r["chunk_index"],
                            "content":   r["content"],
                            "sheet_gid": r["sheet_gid"],
                            "sheet_name": r["sheet_name"],
                        }
                        for r in rows
                    ]
                    first = rows[0]
                    return {
                        "file_id":    file_id,
                        "drive_id":   drive_id,
                        "drive_name": drive_name,
                        "title":      first["title"],
                        "owner":      first["owner"],
                        "source_url": first["source_url"],
                        "file_type":  first["file_type"],
                        "modified_at": str(first["drive_modified_at"]) if first["drive_modified_at"] else None,
                        "folder_path": first["folder_path"],
                        "chunks":     chunks,
                        "full_text":  "\n".join(r["content"] for r in rows if r["content"]),
                    }
    finally:
        conn.close()
    return {"error": "file not found in MCP-enabled drives"}


@mcp.tool()
def stats() -> dict:
    """Global index statistics across MCP-enabled drives."""
    conn = db.connect()
    try:
        scoped = db.search_enabled_schemas(conn)
        by_drive = []
        total_chunks = 0
        total_files = 0
        for drive_id, name in scoped:
            schema = db.schema_for_drive(drive_id)
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT COUNT(*) AS c FROM {db._quote_ident(schema)}.documents"
                )
                r = cur.fetchone()
                c = int(r["c"]) if r else 0
            by_drive.append({"drive_id": drive_id, "name": name, "chunks": c})
            total_chunks += c
        # Approx file count from registry
        for f in db.list_fds(conn):
            if f.get("search_enabled"):
                total_files += f.get("file_count") or 0
    finally:
        conn.close()
    return {
        "drives_in_scope": len(scoped),
        "total_chunks": total_chunks,
        "total_files_approx": total_files,
        "by_drive": by_drive,
    }


# ---------------------------------------------------------------------
# HTTP Basic Auth middleware (checks against public.mcp_users)
# ---------------------------------------------------------------------
class BasicAuthMiddleware(BaseHTTPMiddleware):
    """HTTP Basic Auth gate — every MCP request must carry a valid
    `Authorization: Basic <b64(user:pass)>` matched against `mcp_users`."""

    async def dispatch(self, request: Request, call_next):
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("basic "):
            return self._challenge()
        try:
            raw = base64.b64decode(auth.split(None, 1)[1]).decode("utf-8")
            username, _, password = raw.partition(":")
        except Exception:
            return self._challenge()

        if not username or not password:
            return self._challenge()

        # Verify against mcp_users
        try:
            conn = db.connect()
            try:
                stored = db.get_mcp_user_hash(conn, username)
            finally:
                conn.close()
        except Exception as e:
            log.exception("mcp_users lookup failed: %s", e)
            return JSONResponse({"error": "auth backend unavailable"}, status_code=503)

        if not stored or not verify_password(password, stored):
            return self._challenge()

        return await call_next(request)

    @staticmethod
    def _challenge() -> Response:
        return Response(
            status_code=401,
            headers={"www-authenticate": 'Basic realm="WinServerRAG MCP"'},
            content=b"unauthorized",
        )


def build_mcp_app() -> Starlette:
    """Return a Starlette ASGI sub-app that handles MCP over Streamable HTTP,
    gated by Basic Auth. Mount this under /mcp in the main FastAPI app."""
    # FastMCP provides the Streamable HTTP ASGI app. We wrap it in our Basic
    # Auth middleware so every /mcp request must authenticate.
    inner = mcp.streamable_http_app()
    app = Starlette(
        routes=[Mount("/", app=inner)],
        middleware=[Middleware(BasicAuthMiddleware)],
    )
    return app
