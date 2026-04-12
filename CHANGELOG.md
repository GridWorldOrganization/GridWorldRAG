# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [0.1.3] - 2026-04-12

### Fixed

- **Monitor display corruption (20-30%)** — `tput sc/rc` replaced with relative cursor-up (`\033[NA]` + line count tracking). Python subprocesses could emit ESC 7, overwriting the terminal's saved cursor position.
- **Lockfile stale detection** — `_acquire_lock()` now uses `os.kill(pid, 0)` PID liveness probe before falling back to mtime-based 20-minute stale check. Dead PIDs are taken over immediately instead of waiting.
- **OAuth token refresh fragility** — `creds.refresh(Request())` wrapped in `_api_call_with_retry()` to survive transient 5xx / connection reset from oauth2.googleapis.com.

### Added

- **3-phase build** — `build_parallel.py` restructured into `--fetch-only` (Phase 1), `--split-only` (Phase 2, new), `--work-only` (Phase 3). Phase 2 splits tasks from filelist.pkl into taskdata.pkl, enabling Phase 1 skip on resume.
- **Resume prompt** — `run_build.sh` now prompts `[Y/n]` when filelist.pkl exists. Y (default) skips Phase 1, N re-fetches (needed after whitelist changes).
- **`/tmp` disk space preflight** — `_preflight_tmp_disk_space()` checks `shutil.disk_usage("/tmp")` against `BUILD_MIN_TMP_FREE_BYTES` (default 500MB) before pickle writes. Insufficient space exits with code 2.
- **`sync_history` MCP tool** — returns sync_rotate execution history with aggregated stats for the past N days.
- **11 new tests** — OAuth refresh retry (4), /tmp preflight (5), lockfile PID probe (2 updated). Total: 82 tests across 11 files.

### Changed

- **`TASK_SPLIT_THRESHOLD`** default remains 5000 in code but can be tuned in config.env (user tested with 200-500 for better parallelism with 6 workers).
- **GitHub Issues #10-12** created and closed — PID probe, OAuth retry, /tmp preflight.

## [0.1.2] - 2026-04-11

### Fixed

- **SQL LIKE wildcard bug** — Drive file IDs contain `_` which is a LIKE metacharacter in PostgreSQL, causing `delete_by_file_id` and `lookup_by_url` to silently match unrelated rows. Added `_escape_like_literal()` helper and `ESCAPE '\'` to every LIKE query. Verified empirically: `'ABXCD' LIKE 'AB_CD%'` returns TRUE.
- **Non-atomic delete+insert** — `upsert_file_chunks(conn, file_id, chunks)` now wraps the delete + insert of a single file in one transaction, rolling back cleanly on failure.
- **Daemon thread SSL anti-pattern** — deleted the old `sync.py` which still used `threading.Thread(daemon=True)` for per-file timeout (documented as forbidden in CLAUDE.md). `sync_rotate.py` correctly uses httplib2 socket timeouts.

### Added

- **Rotation-based differential sync** (`sync_rotate.py`) — iterates each shared drive independently using drive-scoped Changes API tokens. Fast path: 22 drives × 1 Changes API call each, ~7 seconds when no changes. Embedding model loads lazily only when changes are detected.
- **launchd LaunchAgent** (`launchd/co.gridworld.gridworldrag.sync.plist`) — 5-minute interval with sleep-aware `RunAtLoad` and `Nice=5`. Install via `launchctl load ~/Library/LaunchAgents/...`.
- **Log rotation** — `sync_rotate.py` writes to `~/Library/Logs/gridworldrag/sync_rotate.log` via `logging.handlers.RotatingFileHandler` (5MB × 3 backups, ~15MB total). Replaces all `print()` calls with structured logger output.
- **Disk space pre-flight check** — `shutil.disk_usage()` on the PostgreSQL data directory with a 1GB threshold. Insufficient space aborts the run with exit(2) without advancing any Changes API token, and records a `disk_full_preflight` marker.
- **DiskFull exception handling** — `_is_disk_full_error()` detects `psycopg2.errors.DiskFull` plus "no space"/"disk full"/"out of space" message strings. Raises `DiskFullHalt` to halt the drive loop cleanly.
- **Failed-files retry queue** — files that error during a run enter `sync_state.failed_files` as JSON, and are retried first on the next run via `files().get()` before the normal per-drive loop. Token advancement is now safe because errored files cannot be silently skipped.
- **First test suite** (`tests/`) — 30 unit tests across 6 files covering LIKE escape, lockfile behavior, Drive API field string validity, disk space check, log rotation, and retry queue helpers. No DB or Drive API mocking required — all pure logic.
- **`src/db.py` new helpers** — `upsert_file_chunks(conn, file_id, chunks)`, `file_exists(conn, file_id)`, `_escape_like_literal(value)`. `insert_chunks` and `delete_by_file_id` now accept `commit=False` for composition inside `upsert_file_chunks`.
- **`src/drive_client.py` unified Changes API** — `list_changes(service, token, drive_id=None)` replaces the duplicated `list_changes` + `list_changes_for_drive` pair. Field string now includes `trashed` and `permissions(...)` consistently.
- **Lazy MCP embedding model load** (`gridworld-rag-mcp/server.py`) — SentenceTransformer now loads on first `search()` instead of at startup, avoiding MCP handshake timeout.
- **`GridWorldRAG-secrets` sibling repo workflow** — private companion repository for `config.env` and `shared_drives_whitelist.txt`, connected via symlinks. Documented under "内部運用" in README.
- **Project `CLAUDE.md`** — architecture notes, gotchas, and resilience docs for sync_rotate. Added to repo root.

### Removed

- `sync.py` — replaced by `sync_rotate.py` (per-drive rotation with safer error handling)
- `run_sync.sh` — replaced by `run_sync_rotate.sh`
- Legacy `list_changes_for_drive` / `get_changes_start_token_for_drive` in favor of unified `drive_id=None` parameter

## [0.1.1] - 2026-03-31

### Fixed

- **SSL crash fix** — replaced daemon thread timeout with httplib2 socket-level timeout (`Http(timeout=N)`) to prevent SSL double-free (SIGABRT) and NULL deref (SIGSEGV) when abandoned threads accessed the same SSL socket
- **Duplicate OpenSSL elimination** — switched from `psycopg2-binary` (bundled OpenSSL) to `psycopg2` (source build linking system OpenSSL) to avoid two libssl instances in the same process
- **DB cursor leak fix** — all cursor operations in `db.py` now use `try/finally` to ensure `cur.close()`; write operations rollback on exception
- **Worker crash resilience** — `_worker` now wraps `conn.close()` in `try/finally`; `insert_chunks` failures trigger DB reconnect instead of crashing the worker
- **Spreadsheet exception handling** — broadened catch from `(socket.timeout, OSError)` to `Exception` to cover httplib2-specific errors
- **Worker fatal error reporting** — separated `_worker` into init + `_worker_main` so fatal errors are logged and `results_queue` always receives a response (prevents parent deadlock)
- **Resume support** — `_process_file` checks DB for existing data before processing; re-running the build skips already-indexed files instantly and processes only new/unprocessed files

### Added

- **Work stealing** — busy workers detect idle workers and redistribute 80% of remaining files as sub-tasks; eliminates the "2 workers stuck, 5 idle" problem
- **Resume support** — re-running the build skips already-indexed files instantly (`resumeSkip` in logs and monitor)
- **File listing optimization** — probe drives with single API call to estimate size, sort largest-first, configurable `FETCH_THREADS` (default 3)
- **Monitor improvements** — `skipping(N)` display during resume, `(レート制限待ちNs)` for stalled workers, worker PID logging
- **Zombie prevention** — `atexit` handler, double Ctrl+C force-kill, join with timeout on all exit paths
- **Comprehensive logging** — Phase 1 probe results/sort order/per-drive completion to log file via stderr

### Removed

- `_download_with_sigalrm` / `_DownloadTimeoutError` — daemon thread timeout mechanism (replaced by httplib2 socket timeout)
- `_model_lock` — no longer needed without thread contention in workers
- `docs/debug_talk.md` — development debug log (historical, removed from repo)

## [0.1.0] - 2026-03-30

Initial public release.

### Features

- **Parallel index build** (`build_parallel.py`) — multi-worker task queue with real-time monitor
- **Differential sync** (`sync.py`) — incremental updates via Google Drive Changes API
- **MCP server** (`gridworld-rag-mcp/server.py`) — `search`, `lookup`, `stats`, `folder_tree`, `recent_changes` tools for Claude Code
- **Google Drive support** — Docs, Sheets (per-sheet via Sheets API), Slides, PDF, images (OCR optional), video/audio metadata
- **Shared Drive whitelist** — explicit allowlist for target drives
- **Folder path indexing** — reconstructs folder hierarchy from Drive file list without extra API calls
- **DB transfer** (`export_db.sh` / `import_db.sh`) — deploy indexed DB to machines without Google Drive access
- **Worker status monitoring** — per-worker progress bar with rate-limit detection
- **Timeout handling** — httplib2 socket-level timeout to prevent CLOSE_WAIT hangs
