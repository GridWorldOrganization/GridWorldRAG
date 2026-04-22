-- WinServerRAG database schema.
--
-- Layout:
--   public.fd_registry    : list of shared drives (ON/OFF, state, counters)
--   fd_<drive_id>.*       : per-shared-drive schema created on demand
--
-- Extensions are created on the 'public' schema.
CREATE EXTENSION IF NOT EXISTS vector;

-- =====================================================================
-- public: registry of known shared drives (Folder Drives -> "fd_*")
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.fd_registry (
    drive_id        TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT FALSE,     -- build/index toggle (admin)
    search_enabled  BOOLEAN NOT NULL DEFAULT FALSE,     -- MCP search scope toggle (end user)
    state           TEXT NOT NULL DEFAULT 'idle',        -- idle | building | syncing | error | disabled
    last_sync_at    TIMESTAMPTZ,
    last_build_at   TIMESTAMPTZ,
    file_count      INTEGER NOT NULL DEFAULT 0,
    chunk_count     INTEGER NOT NULL DEFAULT 0,
    rotate_token    TEXT,
    failed_files    JSONB NOT NULL DEFAULT '[]'::jsonb,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Migration: add column if table already existed without it
ALTER TABLE public.fd_registry ADD COLUMN IF NOT EXISTS search_enabled BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_fd_registry_enabled ON public.fd_registry (enabled);
CREATE INDEX IF NOT EXISTS idx_fd_registry_search_enabled ON public.fd_registry (search_enabled);

-- =====================================================================
-- public: MCP login users (for Claude Cowork remote access)
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.mcp_users (
    username       TEXT PRIMARY KEY,
    password_hash  TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Per-user MCP search scope. A row means the user is allowed to search that
-- drive. `public.fd_registry.search_enabled` is retained for display only
-- (as a "any user can search this?" flag) and is not used for authorization.
CREATE TABLE IF NOT EXISTS public.mcp_user_drives (
    username    TEXT NOT NULL,
    drive_id    TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (username, drive_id),
    FOREIGN KEY (username) REFERENCES public.mcp_users(username) ON DELETE CASCADE,
    FOREIGN KEY (drive_id) REFERENCES public.fd_registry(drive_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_mcp_user_drives_username ON public.mcp_user_drives (username);

-- =====================================================================
-- public: MCP query log — every tool invocation is recorded here
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.mcp_query_log (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    username        TEXT,
    tool_name       TEXT NOT NULL,
    query           TEXT,
    returned_count  INTEGER,
    returned_ids    JSONB,
    latency_ms      INTEGER,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_mcp_query_log_ts ON public.mcp_query_log (ts DESC);

-- =====================================================================
-- public: runtime config (kv)
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.daemon_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =====================================================================
-- public: live state of build workers (N threads, configurable)
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.daemon_workers (
    worker_id     INTEGER PRIMARY KEY,
    drive_id      TEXT,
    drive_name    TEXT,
    state         TEXT NOT NULL DEFAULT 'idle',     -- idle | claiming | listing | building | syncing | done | error
    phase         TEXT,                             -- fetching_changes | listing_files | processing_file | committing
    current_file  TEXT,
    files_done    INTEGER NOT NULL DEFAULT 0,
    total_files   INTEGER NOT NULL DEFAULT 0,
    started_at    TIMESTAMPTZ,
    heartbeat_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error    TEXT
);

-- =====================================================================
-- public: daemon event log (append-only, for monitor tail)
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.daemon_events (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    drive_id    TEXT,
    level       TEXT NOT NULL,                -- info | warn | error
    event       TEXT NOT NULL,                -- sweep_start, build_start, file_ok, file_fail, ...
    message     TEXT,
    extra       JSONB
);
CREATE INDEX IF NOT EXISTS idx_daemon_events_ts ON public.daemon_events (ts DESC);
CREATE INDEX IF NOT EXISTS idx_daemon_events_drive ON public.daemon_events (drive_id, ts DESC);
