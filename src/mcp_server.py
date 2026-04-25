"""MCP (Model Context Protocol) server for Claude Cowork / Desktop clients.

This exposes the WinServerRAG index over Streamable HTTP so a remote Claude
can search the user's Google Drive shared folders.

Auth: HTTP Basic Auth keyed on public.mcp_users (see src/mcp_auth.py).
Only drives with `search_enabled = TRUE` are ever searched — the admin
controls this via the Windows monitor's MCP tab.
"""
from __future__ import annotations

import base64
import contextvars
import functools
import logging
import threading
import time
from typing import Optional

# Set by BasicAuthMiddleware on every authenticated request so tool
# implementations can know which MCP user is calling.
_current_user: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "winserverrag_mcp_current_user", default=None
)

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount

from src import db
from src.config import (
    EMBEDDING_MODEL, EMBEDDING_DEVICE, ENABLE_RERANKER, RERANKER_CANDIDATE_K,
)
from src.mcp_auth import verify_password
from src.reranker import rerank as _rerank

log = logging.getLogger("mcp_server")

# ---------------------------------------------------------------------
# Embedding (lazy, per-process cache, thread-safe init)
# ---------------------------------------------------------------------
_model = None
_model_lock = threading.Lock()  # Fix D: double-init guard


def _get_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        # Re-check under lock: another thread may have populated while we waited.
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
from mcp.server.transport_security import TransportSecuritySettings

# FastMCP auto-enables DNS rebinding protection when host is localhost,
# which breaks when reached via cloudflared/ngrok tunnels. Our Basic Auth
# middleware is already the gate — disable the Host-based check so a
# tunnel's random subdomain doesn't get rejected with "Invalid Host header".
_transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=False,
)

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
    transport_security=_transport_security,
)


# Decorator that persists every tool invocation to public.mcp_query_log.
# Captures the query (for search), the returned drive_id/file_id set when
# available, latency in ms, and any exception string.
def _logged_tool(tool_name: str):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.time()
            user = _current_user.get()
            query = kwargs.get("query") or (args[0] if args and tool_name == "search" else None)
            url = kwargs.get("url") or (args[0] if args and tool_name == "lookup" else None)
            query_for_log = query if isinstance(query, str) else url if isinstance(url, str) else None
            err = None
            result = None
            try:
                result = fn(*args, **kwargs)
                return result
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                raise
            finally:
                latency_ms = int((time.time() - t0) * 1000)
                returned_count = None
                returned_ids = None
                if isinstance(result, dict):
                    if "count" in result and isinstance(result["count"], int):
                        returned_count = result["count"]
                    rs = result.get("results")
                    if isinstance(rs, list):
                        returned_ids = [
                            {"drive_id": r.get("drive_id"),
                             "title":    (r.get("title") or "")[:80]}
                            for r in rs[:10]
                        ]
                    elif "drives" in result and isinstance(result["drives"], list):
                        returned_ids = [d.get("drive_id") for d in result["drives"][:20]]
                try:
                    conn = db.connect()
                    try:
                        db.log_mcp_query(
                            conn,
                            username=user, tool_name=tool_name,
                            query=query_for_log,
                            returned_count=returned_count,
                            returned_ids=returned_ids,
                            latency_ms=latency_ms,
                            error=err,
                        )
                    finally:
                        conn.close()
                except Exception as e:
                    log.warning("mcp query log persist failed: %s", e)
        return wrapper
    return deco


def _allowed_schemas(conn) -> list[tuple[str, str]]:
    """Resolve (drive_id, drive_name) pairs the caller is allowed to search.

    As of v0.6.2 the scope is PER-USER again. Each MCP user (basic-auth
    username) has their own row set in public.mcp_user_drives, and the
    search / list_drives tools filter by _current_user. An authenticated
    user with no rows in mcp_user_drives sees an empty scope — they can
    still call the tools, they just get no results until the admin adds
    drives on the Windows monitor's MCP tab.
    """
    username = _current_user.get()
    if not username:
        return []
    return db.search_enabled_schemas_for_user(conn, username)


@mcp.tool()
@_logged_tool("list_drives")
def list_drives() -> dict:
    """List shared drives currently in MCP search scope.

    Scope is global — the admin toggles which drives are searchable on the
    Windows monitor's MCP tab. Empty list means no drive has been enabled yet.
    """
    username = _current_user.get()
    conn = db.connect()
    try:
        allowed_ids = {d for d, _ in _allowed_schemas(conn)}
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
        for f in fds if f["drive_id"] in allowed_ids
    ]
    return {"drives": scoped, "count": len(scoped), "user": username}


@mcp.tool()
@_logged_tool("search")
def search(query: str, n_results: int = 10, owner: Optional[str] = None) -> dict:
    """Semantic search across all MCP-enabled shared drives.

    Args:
        query: natural-language query (Japanese or English).
        n_results: maximum number of chunks to return. 1..50. Default 10.
        owner: optional email-address filter — only returns chunks owned by
               that person.
    """
    n_results = max(1, min(50, int(n_results)))
    username = _current_user.get()
    # When reranking, pull more candidates from the vector search than the
    # final top-N so the reranker has room to reorder.
    candidate_k = RERANKER_CANDIDATE_K if ENABLE_RERANKER else n_results
    candidate_k = max(n_results, candidate_k)
    conn = db.connect()
    try:
        schemas = _allowed_schemas(conn)
        if not schemas:
            return {
                "results": [],
                "user": username,
                "note": ("No drives are in MCP scope. Ask the administrator to "
                         "enable drives on the Windows monitor MCP tab."),
            }
        emb = _embed_query(query)
        rows = db.search_across_schemas(conn, emb, schemas,
                                        n_results=candidate_k, owner=owner)
    finally:
        conn.close()
    drive_names = {d: n for d, n in schemas}
    candidates = [
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
    # Rerank: swaps the order and trims to n_results. Pass-through if disabled.
    results = _rerank(query, candidates, top_n=n_results, text_key="content")
    return {"results": results, "count": len(results), "query": query,
            "reranked": ENABLE_RERANKER}


@mcp.tool()
@_logged_tool("lookup")
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
        schemas = _allowed_schemas(conn)
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
@_logged_tool("stats")
def stats() -> dict:
    """Index statistics for the drives this MCP user can search."""
    conn = db.connect()
    try:
        scoped = _allowed_schemas(conn)
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

        # Make the username available to tool implementations via contextvar.
        token = _current_user.set(username)
        try:
            return await call_next(request)
        finally:
            _current_user.reset(token)

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
