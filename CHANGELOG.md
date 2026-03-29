# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

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
- **Timeout handling** — per-file threading timeout to prevent CLOSE_WAIT hangs
