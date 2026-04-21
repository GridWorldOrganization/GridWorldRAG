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

-- =====================================================================
-- public: runtime config (kv) — used for live worker count (1..10)
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
