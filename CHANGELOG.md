# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [0.1.1] - 2026-03-31

### Fixed

- **SSL crash fix** — replaced daemon thread timeout with httplib2 socket-level timeout (`Http(timeout=N)`) to prevent SSL double-free (SIGABRT) and NULL deref (SIGSEGV) when abandoned threads accessed the same SSL socket
- **Duplicate OpenSSL elimination** — switched from `psycopg2-binary` (bundled OpenSSL) to `psycopg2` (source build linking system OpenSSL) to avoid two libssl instances in the same process
- **DB cursor leak fix** — all cursor operations in `db.py` now use `try/finally` to ensure `cur.close()`; write operations rollback on exception
- **Worker crash resilience** — `_worker` now wraps `conn.close()` in `try/finally`; `insert_chunks` failures trigger DB reconnect instead of crashing the worker
- **Spreadsheet exception handling** — broadened catch from `(socket.timeout, OSError)` to `Exception` to cover httplib2-specific errors
- **Worker fatal error reporting** — separated `_worker` into init + `_worker_main` so fatal errors are logged and `results_queue` always receives a response (prevents parent deadlock)

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
