"""FastAPI control API + static web monitor.

Endpoints:
  GET    /api/fds                 list registry
  POST   /api/fds/{drive_id}/enable
  POST   /api/fds/{drive_id}/disable
  POST   /api/fds/{drive_id}/sync-now
  POST   /api/fds/{drive_id}/rebuild
  DELETE /api/fds/{drive_id}
  GET    /api/drives/available    list Google shared drives (registry sync)
  GET    /api/stats
  GET    /api/events              tail events (last 200)
  WS     /ws/events               streaming tail
  GET    /                        static web monitor

Daemon workers run in background threads within the API process
so manual sync-now / rebuild can be triggered via the API.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request, Depends
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src import config, db
from src import drive_client as dc
from src.logging_setup import setup_logger
from src.rag_daemon import build_full, sync_delta

log = setup_logger("control_api")

# ---------------------------------------------------------------------
# Globals: one drive service per process, one worker pool
# ---------------------------------------------------------------------
_service = None
_service_lock = threading.Lock()
_workers_lock = threading.Lock()
_workers: dict[str, threading.Thread] = {}

# In-memory event broadcaster (tail-style); pulls from DB daemon_events table
_subscribers: set[asyncio.Queue] = set()
_sub_lock = asyncio.Lock()


def _get_service():
    global _service
    with _service_lock:
        if _service is None:
            _service = dc.authenticate()
    return _service


def _run_worker(target, drive_id: str) -> None:
    def _runner():
        try:
            conn = db.connect()
            try:
                target(conn, drive_id, _get_service())
            finally:
                conn.close()
        except Exception as e:
            log.error("worker %s failed: %s", target.__name__, e)
        finally:
            with _workers_lock:
                _workers.pop(drive_id, None)

    with _workers_lock:
        existing = _workers.get(drive_id)
        if existing and existing.is_alive():
            raise HTTPException(status_code=409, detail=f"operation already running for {drive_id}")
        t = threading.Thread(target=_runner, name=f"worker-{drive_id}", daemon=True)
        _workers[drive_id] = t
        t.start()


# ---------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------
async def require_token(request: Request):
    """Enforce localhost-only when no bearer token is configured.

    Previous behavior silently allowed all callers if API_BEARER_TOKEN was
    empty — that's fine for the default API_HOST=127.0.0.1 but becomes an
    unauthenticated open API if someone flips the host to 0.0.0.0. Require
    localhost in the no-token case.
    """
    client = request.client.host if request.client else ""
    is_local = client in ("127.0.0.1", "::1", "localhost")
    if not config.API_BEARER_TOKEN:
        if is_local:
            return
        raise HTTPException(status_code=403,
                            detail="API requires API_BEARER_TOKEN for non-localhost access")
    if is_local:
        return
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if auth[7:].strip() != config.API_BEARER_TOKEN:
        raise HTTPException(status_code=401, detail="invalid bearer token")


# ---------------------------------------------------------------------
# App — lifespan that also drives the FastMCP session manager
# ---------------------------------------------------------------------
from contextlib import asynccontextmanager
from src.mcp_server import mcp as _mcp_instance, build_mcp_app as _build_mcp_app


@asynccontextmanager
async def _lifespan(_app):
    # FastMCP's streamable HTTP requires its session_manager to be "running"
    # across the lifetime of the app. Sub-app lifespans aren't always
    # propagated through Mount, so we open it explicitly here.
    async with _mcp_instance.session_manager.run():
        # Seed MCP default users (idempotent, fast)
        try:
            db.ensure_database()
            conn = db.connect()
            try:
                created = db.seed_default_mcp_users(conn)
                if created:
                    log.info("seeded default MCP users: %s", ", ".join(created))
            finally:
                conn.close()
        except Exception as e:
            log.error("startup mcp-user seed failed (non-fatal): %s", e)
        # Background pumps
        asyncio.create_task(_event_pump())
        asyncio.create_task(_stats_pump())
        yield


app = FastAPI(title="WinServerRAG Control API", version="0.2.0", lifespan=_lifespan)

_web_root = config.PROJECT_ROOT / "web"
if _web_root.exists():
    app.mount("/static", StaticFiles(directory=str(_web_root / "static")), name="static")

# Mount the MCP Streamable-HTTP server under /mcp, gated by Basic Auth
# against public.mcp_users. The FastMCP session manager is started by the
# lifespan context above.
try:
    app.mount("/mcp", _build_mcp_app())
    log.info("MCP endpoint mounted at /mcp")
except Exception as _e:
    log.error("MCP mount failed (non-fatal): %s", _e)


# NOTE: startup/shutdown is handled by the `lifespan` function above so the
# FastMCP session manager gets a proper run-context.


@app.get("/")
def index():
    f = _web_root / "index.html"
    if not f.exists():
        return {"detail": "web UI not installed"}
    return FileResponse(str(f))


# ---------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------
class FDView(BaseModel):
    drive_id: str
    name: str
    enabled: bool
    search_enabled: bool
    state: str
    last_sync_at: Optional[datetime]
    last_build_at: Optional[datetime]
    file_count: int
    chunk_count: int
    last_error: Optional[str]
    running: bool = False


class StatsView(BaseModel):
    total_fds: int
    enabled_fds: int
    total_files: int
    total_chunks: int
    db_size_bytes: int
    pg_ok: bool
    drive_ok: bool


# ---------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------
@app.get("/api/fds", dependencies=[Depends(require_token)])
def api_list_fds():
    conn = db.connect()
    try:
        rows = db.list_fds(conn)
    finally:
        conn.close()
    with _workers_lock:
        running_ids = {k for k, t in _workers.items() if t.is_alive()}
    return [
        FDView(**{**r, "running": r["drive_id"] in running_ids}).model_dump()
        for r in rows
    ]


@app.post("/api/fds/{drive_id}/enable", dependencies=[Depends(require_token)])
def api_enable(drive_id: str):
    conn = db.connect()
    try:
        row = db.get_fd(conn, drive_id)
        if not row:
            raise HTTPException(status_code=404, detail="unknown drive_id")
        db.set_enabled(conn, drive_id, True)
    finally:
        conn.close()
    return {"ok": True, "drive_id": drive_id, "enabled": True}


@app.post("/api/fds/{drive_id}/disable", dependencies=[Depends(require_token)])
def api_disable(drive_id: str):
    conn = db.connect()
    try:
        row = db.get_fd(conn, drive_id)
        if not row:
            raise HTTPException(status_code=404, detail="unknown drive_id")
        db.set_enabled(conn, drive_id, False)
    finally:
        conn.close()
    return {"ok": True, "drive_id": drive_id, "enabled": False}


@app.post("/api/fds/{drive_id}/search-enable", dependencies=[Depends(require_token)])
def api_search_enable(drive_id: str):
    conn = db.connect()
    try:
        row = db.get_fd(conn, drive_id)
        if not row:
            raise HTTPException(status_code=404, detail="unknown drive_id")
        db.set_search_enabled(conn, drive_id, True)
    finally:
        conn.close()
    return {"ok": True, "drive_id": drive_id, "search_enabled": True}


@app.post("/api/fds/{drive_id}/search-disable", dependencies=[Depends(require_token)])
def api_search_disable(drive_id: str):
    conn = db.connect()
    try:
        row = db.get_fd(conn, drive_id)
        if not row:
            raise HTTPException(status_code=404, detail="unknown drive_id")
        db.set_search_enabled(conn, drive_id, False)
    finally:
        conn.close()
    return {"ok": True, "drive_id": drive_id, "search_enabled": False}


@app.post("/api/fds/{drive_id}/sync-now", dependencies=[Depends(require_token)])
def api_sync_now(drive_id: str):
    conn = db.connect()
    try:
        row = db.get_fd(conn, drive_id)
        if not row:
            raise HTTPException(status_code=404, detail="unknown drive_id")
    finally:
        conn.close()
    _run_worker(sync_delta, drive_id)
    return {"ok": True, "drive_id": drive_id, "started": "sync-now"}


@app.post("/api/fds/{drive_id}/rebuild", dependencies=[Depends(require_token)])
def api_rebuild(drive_id: str):
    # full rebuild: reset rotate_token and counts, then run build_full
    conn = db.connect()
    try:
        row = db.get_fd(conn, drive_id)
        if not row:
            raise HTTPException(status_code=404, detail="unknown drive_id")
        # drop schema so we rebuild fresh
        db.drop_fd_schema(conn, drive_id)
        db.set_rotate_token(conn, drive_id, None)
    finally:
        conn.close()
    _run_worker(build_full, drive_id)
    return {"ok": True, "drive_id": drive_id, "started": "rebuild"}


@app.delete("/api/fds/{drive_id}", dependencies=[Depends(require_token)])
def api_delete(drive_id: str):
    conn = db.connect()
    try:
        row = db.get_fd(conn, drive_id)
        if not row:
            raise HTTPException(status_code=404, detail="unknown drive_id")
        db.delete_fd(conn, drive_id)
    finally:
        conn.close()
    return {"ok": True, "drive_id": drive_id, "deleted": True}


@app.get("/api/drives/available", dependencies=[Depends(require_token)])
def api_available_drives():
    """Ask Google for shared-drive list, upsert into registry, return merged list."""
    service = _get_service()
    drives = dc.list_shared_drives(service)
    conn = db.connect()
    try:
        for d in drives:
            db.upsert_fd(conn, d["id"], d["name"])
        rows = {r["drive_id"]: r for r in db.list_fds(conn)}
    finally:
        conn.close()
    # Return fresh list from registry (now includes newly discovered ones)
    return list(rows.values())


@app.get("/api/workers", dependencies=[Depends(require_token)])
def api_workers():
    """Live worker state + target count."""
    conn = db.connect()
    try:
        workers = db.list_workers(conn)
        try:
            target_s = db.get_config(conn, "worker_count",
                                     str(config.DAEMON_WORKER_THREADS))
            target = int(target_s) if target_s else config.DAEMON_WORKER_THREADS
        except Exception:
            target = config.DAEMON_WORKER_THREADS
    finally:
        conn.close()
    return {
        "target": target,
        "min": 1,
        "max": 10,
        "live": len(workers),
        "workers": workers,
    }


class ScalePayload(BaseModel):
    n: int


class UserCreatePayload(BaseModel):
    username: str
    password: str


class PasswordPayload(BaseModel):
    password: str


@app.post("/api/workers/scale", dependencies=[Depends(require_token)])
def api_workers_scale(payload: ScalePayload):
    n = max(1, min(10, int(payload.n)))
    conn = db.connect()
    try:
        db.set_config(conn, "worker_count", str(n))
    finally:
        conn.close()
    return {"ok": True, "target": n}


# -------- MCP users (login credentials for Claude Cowork access) --------
import re as _re
_USERNAME_RE = _re.compile(r"^[A-Za-z0-9._\-]{1,64}$")


def _validate_username(username: str) -> None:
    if not _USERNAME_RE.match(username or ""):
        raise HTTPException(status_code=400,
                            detail="username must match [A-Za-z0-9._-], 1..64 chars")


def _validate_password(password: str) -> None:
    if not password or len(password) < 4:
        raise HTTPException(status_code=400, detail="password must be at least 4 chars")
    if len(password) > 256:
        raise HTTPException(status_code=400, detail="password too long")


@app.get("/api/mcp/users", dependencies=[Depends(require_token)])
def api_list_mcp_users():
    conn = db.connect()
    try:
        return db.list_mcp_users(conn)
    finally:
        conn.close()


@app.post("/api/mcp/users", dependencies=[Depends(require_token)])
def api_create_mcp_user(payload: UserCreatePayload):
    """Create a new user OR update the password of an existing one (upsert)."""
    _validate_username(payload.username)
    _validate_password(payload.password)
    from src.mcp_auth import hash_password
    conn = db.connect()
    try:
        db.upsert_mcp_user(conn, payload.username, hash_password(payload.password))
    finally:
        conn.close()
    return {"ok": True, "username": payload.username}


@app.put("/api/mcp/users/{username}/password", dependencies=[Depends(require_token)])
def api_change_mcp_password(username: str, payload: PasswordPayload):
    _validate_username(username)
    _validate_password(payload.password)
    from src.mcp_auth import hash_password
    conn = db.connect()
    try:
        if db.get_mcp_user_hash(conn, username) is None:
            raise HTTPException(status_code=404, detail="unknown user")
        db.upsert_mcp_user(conn, username, hash_password(payload.password))
    finally:
        conn.close()
    return {"ok": True, "username": username}


@app.delete("/api/mcp/users/{username}", dependencies=[Depends(require_token)])
def api_delete_mcp_user(username: str):
    _validate_username(username)
    conn = db.connect()
    try:
        n = db.delete_mcp_user(conn, username)
    finally:
        conn.close()
    if n == 0:
        raise HTTPException(status_code=404, detail="unknown user")
    return {"ok": True, "username": username}


# --- stats cache: refreshed asynchronously in the background so /api/stats
# --- is a pure in-memory read. Under heavy daemon load PG can take seconds
# --- for even SELECT 1, so we must never block request handlers on it.
_stats_cache: dict = {
    "total_fds": 0, "enabled_fds": 0, "total_files": 0, "total_chunks": 0,
    "db_size_bytes": None, "pg_ok": False, "drive_ok": True,
    "last_updated": None,
}
_stats_lock = threading.Lock()


async def _stats_pump():
    """Background task: refresh _stats_cache every 5s from DB."""
    import asyncio as _a
    while True:
        await _a.sleep(5.0)
        try:
            # Run blocking DB work in a thread
            def _fetch():
                conn = db.connect()
                try:
                    return db.global_stats(conn)
                finally:
                    conn.close()
            row = await _a.to_thread(_fetch)
            with _stats_lock:
                _stats_cache.update(row)
                _stats_cache["pg_ok"] = True
                _stats_cache["last_updated"] = _time.time()
        except Exception:
            with _stats_lock:
                _stats_cache["pg_ok"] = False
                _stats_cache["last_updated"] = _time.time()


@app.get("/api/stats", dependencies=[Depends(require_token)])
def api_stats():
    with _stats_lock:
        return dict(_stats_cache)


@app.get("/api/events", dependencies=[Depends(require_token)])
def api_events(limit: int = 200, drive_id: Optional[str] = None):
    conn = db.connect()
    try:
        return db.tail_events(conn, limit=limit, drive_id=drive_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------
# WebSocket streaming
# ---------------------------------------------------------------------
async def _event_pump():
    """Poll daemon_events and push deltas to subscribers."""
    last_id = 0
    err_consecutive = 0
    while True:
        await asyncio.sleep(1.0)
        try:
            conn = db.connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, ts, drive_id, level, event, message, extra "
                        "FROM public.daemon_events WHERE id > %s ORDER BY id ASC LIMIT 200",
                        (last_id,),
                    )
                    rows = cur.fetchall()
            finally:
                conn.close()
            err_consecutive = 0
        except Exception as e:
            err_consecutive += 1
            if err_consecutive in (1, 10, 100):
                log.warning("event pump db poll failed (%d consecutive): %s",
                            err_consecutive, e)
            continue
        if not rows:
            continue
        for r in rows:
            last_id = max(last_id, r["id"])
            payload = {
                "id": r["id"],
                "ts": r["ts"].isoformat() if r["ts"] else None,
                "drive_id": r["drive_id"],
                "level": r["level"],
                "event": r["event"],
                "message": r["message"],
                "extra": r["extra"],
            }
            msg = json.dumps(payload, ensure_ascii=False, default=str)
            async with _sub_lock:
                dead: list[asyncio.Queue] = []
                for q in _subscribers:
                    try:
                        q.put_nowait(msg)
                    except asyncio.QueueFull:
                        dead.append(q)
                for q in dead:
                    _subscribers.discard(q)


@app.websocket("/ws/events")
async def ws_events(ws: WebSocket):
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    async with _sub_lock:
        _subscribers.add(q)
    try:
        while True:
            msg = await q.get()
            await ws.send_text(msg)
    except WebSocketDisconnect:
        pass
    finally:
        async with _sub_lock:
            _subscribers.discard(q)


# ---------------------------------------------------------------------
# Entrypoint (dev runner)
# ---------------------------------------------------------------------
def main() -> int:
    import uvicorn
    uvicorn.run("src.control_api:app",
                host=config.API_HOST, port=config.API_PORT, reload=False, log_level="info")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
