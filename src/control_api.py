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
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel

from src import config, db
from src import drive_client as dc
from src.logging_setup import setup_logger
# v0.4: the daemon owns build/sync via the shared task queue. The API no
# longer spawns workers; it just flips flags and the daemon picks them up
# on the next manager iteration (≤ 5 s latency).

log = setup_logger("control_api")

# ---------------------------------------------------------------------
# Globals: one drive service per process, one worker pool
# ---------------------------------------------------------------------
_service = None
_service_lock = threading.Lock()

# In-memory event broadcaster (tail-style); pulls from DB daemon_events table
_subscribers: set[asyncio.Queue] = set()
_sub_lock = asyncio.Lock()


def _get_service():
    """Return the cached Drive service. Populated at startup (Fix C).
    Lazy-falls-back to authenticate() on first use if startup pre-auth failed."""
    global _service
    if _service is not None:
        return _service
    with _service_lock:
        if _service is None:
            _service = dc.authenticate()
    return _service


# (v0.4: API no longer spawns worker threads. Daemon owns all build/sync
# work via its shared task queue. The API just flips flags in fd_registry.)


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

        # Fix C: prime the Google Drive service once at startup so requests
        # don't block on OAuth refresh. _get_service() now just returns the
        # cached handle.
        global _service
        try:
            _service = await asyncio.to_thread(dc.authenticate)
            log.info("Drive service pre-authenticated at startup")
        except Exception as e:
            log.warning("Drive pre-auth failed (will lazy-retry on demand): %s", e)

        # Background pumps
        asyncio.create_task(_event_pump())
        asyncio.create_task(_stats_pump())
        asyncio.create_task(_count_estimate_pump())
        yield


app = FastAPI(title="WinServerRAG Control API", version="0.6.0", lifespan=_lifespan)


class _NoCacheStaticMiddleware(BaseHTTPMiddleware):
    """Prevent Chrome from serving a stale index.html / app.js / style.css
    after a WinServerRAG update. /static/* and / get no-cache; /api/* and
    other endpoints are untouched."""
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response


app.add_middleware(_NoCacheStaticMiddleware)

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
    file_count_estimate: Optional[int] = None
    file_count_estimate_at: Optional[datetime] = None
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
        # "running" = daemon has an active worker on this drive
        try:
            active = db.active_drive_ids(conn)
        except Exception:
            active = set()
    finally:
        conn.close()
    return [
        FDView(**{**r, "running": r["drive_id"] in active}).model_dump()
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
    """Forces the daemon to pick this drive up on next manager iteration by
    clearing its state markers. (No worker spawn from API in v0.4.)"""
    conn = db.connect()
    try:
        row = db.get_fd(conn, drive_id)
        if not row:
            raise HTTPException(status_code=404, detail="unknown drive_id")
        # Mark idle so manager will enqueue list_delta (or list_full if no token)
        db.abort_build(conn, drive_id)
    finally:
        conn.close()
    return {"ok": True, "drive_id": drive_id, "started": "sync-now"}


@app.post("/api/fds/{drive_id}/rebuild", dependencies=[Depends(require_token)])
def api_rebuild(drive_id: str):
    """Drop data + clear rotate_token. Daemon will enqueue a fresh list_full
    on its next manager iteration."""
    conn = db.connect()
    try:
        row = db.get_fd(conn, drive_id)
        if not row:
            raise HTTPException(status_code=404, detail="unknown drive_id")
        db.drop_fd_schema(conn, drive_id)
        db.set_rotate_token(conn, drive_id, None)
        db.abort_build(conn, drive_id)
    finally:
        conn.close()
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
    """Live worker state + configured target count (fixed at daemon startup)."""
    conn = db.connect()
    try:
        workers = db.list_workers(conn)
    finally:
        conn.close()
    return {
        "target": config.DAEMON_WORKER_THREADS,
        "min": 1,
        "max": 10,
        "live": len(workers),
        "workers": workers,
    }


class UserCreatePayload(BaseModel):
    username: str
    password: str


class PasswordPayload(BaseModel):
    password: str


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


# (per-user drive matrix removed in v0.3 — search_enabled is global now)


@app.get("/api/eval", dependencies=[Depends(require_token)])
def api_eval_last():
    """Last eval run summary (from daemon_config.eval_last). Null if never run."""
    conn = db.connect()
    try:
        val = db.get_config(conn, "eval_last")
    finally:
        conn.close()
    if not val:
        return {"score": None, "note": "eval not run yet — see tests/eval/"}
    try:
        return json.loads(val)
    except Exception:
        return {"score": None, "note": "eval_last unparseable"}


@app.get("/api/mcp/query-log", dependencies=[Depends(require_token)])
def api_mcp_query_log(limit: int = 50):
    """Recent MCP tool invocations (Cowork queries). Read by the Web UI."""
    limit = max(1, min(500, int(limit)))
    conn = db.connect()
    try:
        return db.tail_mcp_query_log(conn, limit=limit)
    finally:
        conn.close()


# --- stats cache: refreshed asynchronously in the background so /api/stats
# --- is a pure in-memory read. Under heavy daemon load PG can take seconds
# --- for even SELECT 1, so we must never block request handlers on it.
_stats_cache: dict = {
    "total_fds": 0, "enabled_fds": 0, "total_files": 0, "total_chunks": 0,
    "total_files_estimate": 0, "enabled_files_estimate": 0,
    "db_size_bytes": None, "pg_ok": False, "drive_ok": True,
    "last_updated": None,
    # Device info (GPU vs CPU). Static fields are probed once at startup;
    # dynamic VRAM / util are refreshed in _stats_pump via nvidia-smi.
    "device": {
        "kind": "cpu",              # "cuda" or "cpu"
        "name": None,               # e.g. "NVIDIA GeForce RTX 4070 SUPER"
        "total_vram_mb": None,
        "used_vram_mb": None,
        "util_pct": None,
        "power_w": None,
    },
}
_stats_lock = threading.Lock()


def _probe_device_static() -> dict:
    """One-shot probe at startup: GPU name + total VRAM if CUDA is available."""
    try:
        import torch  # imported lazily; ~100ms first time
        if torch.cuda.is_available() and torch.cuda.device_count() > 0:
            props = torch.cuda.get_device_properties(0)
            return {
                "kind": "cuda",
                "name": torch.cuda.get_device_name(0),
                "total_vram_mb": int(props.total_memory / 1024 / 1024),
                "used_vram_mb": None, "util_pct": None, "power_w": None,
            }
    except Exception:
        pass
    return {"kind": "cpu", "name": None, "total_vram_mb": None,
            "used_vram_mb": None, "util_pct": None, "power_w": None}


def _probe_gpu_live() -> dict | None:
    """Cheap nvidia-smi call to grab util/mem/power. Returns None if no GPU."""
    try:
        import subprocess
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode != 0:
            return None
        parts = [p.strip() for p in r.stdout.strip().split(",")]
        if len(parts) < 3:
            return None
        return {
            "util_pct":     int(float(parts[0])),
            "used_vram_mb": int(float(parts[1])),
            "power_w":      int(float(parts[2])),
        }
    except Exception:
        return None


async def _stats_pump():
    """Background task: refresh _stats_cache every 5s from DB + nvidia-smi."""
    import asyncio as _a
    # One-shot static device probe
    static_dev = await _a.to_thread(_probe_device_static)
    with _stats_lock:
        _stats_cache["device"].update(static_dev)
    log.info(f"device probed: kind={static_dev['kind']} name={static_dev.get('name')}")

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

        # GPU live telemetry (only if we detected CUDA at startup)
        if _stats_cache["device"].get("kind") == "cuda":
            live = await _a.to_thread(_probe_gpu_live)
            if live is not None:
                with _stats_lock:
                    _stats_cache["device"].update(live)


@app.get("/api/stats", dependencies=[Depends(require_token)])
def api_stats():
    with _stats_lock:
        return dict(_stats_cache)


# --- file_count_estimate pump --------------------------------------------
# Refreshes fd_registry.file_count_estimate for EVERY known drive (enabled
# or not) so the UI can preview "~N files" before the user turns indexing
# on. This uses the cheap id-only Drive API query — not a full list. Runs
# at startup (staggered so we don't hammer the API) and then every 30 min.
COUNT_REFRESH_INTERVAL_SEC = 1800   # 30 min
COUNT_REFRESH_STAGGER_SEC  = 2      # one drive every N seconds initially


async def _count_estimate_pump():
    """Background: count files in every registered drive periodically."""
    import asyncio as _a
    # Let the lifespan finish priming Drive auth first
    await _a.sleep(10)

    async def _count_one(drive_id: str, name: str):
        def _work():
            conn = db.connect()
            try:
                service = dc.get_drive_service()
                n = dc.count_files_in_drive(service, drive_id)
                db.set_file_count_estimate(conn, drive_id, n)
                return n
            finally:
                conn.close()
        try:
            n = await _a.to_thread(_work)
            log.info(f"file_count_estimate: {name!s:30} = {n}")
        except Exception as e:
            log.warning(f"file_count_estimate failed for {name!s}: {e}")

    while True:
        try:
            def _fetch_drives():
                conn = db.connect()
                try:
                    return [(r["drive_id"], r["name"]) for r in db.list_fds(conn)]
                finally:
                    conn.close()
            drives = await _a.to_thread(_fetch_drives)
        except Exception as e:
            log.warning(f"count pump: list_fds failed: {e}")
            drives = []

        log.info(f"file_count_estimate: sweeping {len(drives)} drives")
        for drive_id, name in drives:
            await _count_one(drive_id, name)
            await _a.sleep(COUNT_REFRESH_STAGGER_SEC)

        await _a.sleep(COUNT_REFRESH_INTERVAL_SEC)


@app.post("/api/fds/{drive_id}/refresh-count",
          dependencies=[Depends(require_token)])
async def api_refresh_count(drive_id: str):
    """Manually re-count files for one drive. Returns the new estimate."""
    import asyncio as _a
    def _work():
        conn = db.connect()
        try:
            service = dc.get_drive_service()
            n = dc.count_files_in_drive(service, drive_id)
            db.set_file_count_estimate(conn, drive_id, n)
            return n
        finally:
            conn.close()
    try:
        n = await _a.to_thread(_work)
        return {"drive_id": drive_id, "file_count_estimate": n}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
