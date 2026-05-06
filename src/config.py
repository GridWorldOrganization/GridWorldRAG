"""Config loader (Windows-native).

UTF-8 is enforced on all file reads (see CLAUDE.md feedback_windows_utf8).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# v1.3.2: honor the WINSERVERRAG_CONFIG_DIR env var that the installer
# (installer/install-services.ps1) sets via NSSM AppEnvironmentExtra.
# Without this, a PyInstaller-bundled service resolves PROJECT_ROOT to
# its frozen bundle root (e.g. C:\Program Files\WinServerRAG\bin\
# winserverrag-daemon\_internal\) and never finds the operator's
# config at %ProgramData%\WinServerRAG\config\config.v2.env. The
# service exits 1 with "no config.env found" and NSSM crash-loops it.
#
# Fallback chain:
#   1) WINSERVERRAG_CONFIG_DIR env var (production: set by installer)
#   2) PROJECT_ROOT/config/                (dev: running from repo)
#
# DATA_ROOT is derived from CONFIG_DIR's parent so logs/lock dirs land
# next to config under %ProgramData%\WinServerRAG\ instead of inside
# the read-only Program Files bundle.
_env_config_dir = os.environ.get("WINSERVERRAG_CONFIG_DIR")
if _env_config_dir:
    CONFIG_DIR = Path(_env_config_dir).resolve()
    DATA_ROOT = CONFIG_DIR.parent
else:
    CONFIG_DIR = PROJECT_ROOT / "config"
    DATA_ROOT = PROJECT_ROOT

# Config path lookup: prefer the versioned v2 file, fall back to legacy.
# Both are tried in order; the first that exists is loaded.
CONFIG_PATH_CANDIDATES = [
    CONFIG_DIR / "config.v2.env",
    CONFIG_DIR / "config.env",
]
CONFIG_PATH: Path | None = None  # set by load_config()


def load_config() -> None:
    global CONFIG_PATH
    path = next((p for p in CONFIG_PATH_CANDIDATES if p.exists()), None)
    if path is None:
        if os.environ.get("WINSERVERRAG_SKIP_CONFIG") == "1":
            return
        shown = " / ".join(str(p) for p in CONFIG_PATH_CANDIDATES)
        print(f"ERROR: no config.env found (looked at: {shown}).",
              file=sys.stderr)
        sys.exit(1)
    CONFIG_PATH = path
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


if os.environ.get("WINSERVERRAG_SKIP_CONFIG") != "1":
    load_config()

    # Required env vars. Fail fast with a clear message instead of letting
    # OAuth/DB calls throw opaque errors later.
    _REQUIRED = [
        "GOOGLE_EMAIL",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "PGPASSWORD",
    ]
    _missing = [k for k in _REQUIRED if not os.environ.get(k)]
    if _missing:
        print(f"ERROR: config.env is missing: {', '.join(_missing)}",
              file=sys.stderr)
        sys.exit(1)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v is not None and v != "" else default


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v is not None and v != "" else default


# --- Google ---
GOOGLE_EMAIL = _env("GOOGLE_EMAIL")
GOOGLE_CLIENT_ID = _env("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_CLIENT_SECRET = _env("GOOGLE_OAUTH_CLIENT_SECRET")
# Token path resolution. Three forms supported:
#   1) Absolute path → used as-is
#   2) Legacy "config/foo" prefix → resolved under DATA_ROOT (= repo
#      root in dev, %ProgramData%\WinServerRAG in prod). Backward compat
#      for installs whose config.v2.env carries the pre-v1.3.2 default.
#   3) Plain relative → resolved under CONFIG_DIR (alongside config.v2.env)
#
# Pre-v1.3.2 always used PROJECT_ROOT, which broke installed services
# (PyInstaller bundle = read-only Program Files subtree).
_token_setting = _env("GOOGLE_TOKEN_PATH", "token.pickle")
_token_path = Path(_token_setting)
if _token_path.is_absolute():
    TOKEN_PATH = _token_path
elif _token_setting.replace("\\", "/").startswith("config/"):
    TOKEN_PATH = DATA_ROOT / _token_setting
else:
    TOKEN_PATH = CONFIG_DIR / _token_setting

# --- PostgreSQL ---
PG_HOST = _env("PGHOST", "localhost")
PG_PORT = _env("PGPORT", "5432")
PG_USER = _env("PGUSER", "postgres")
PG_PASSWORD = _env("PGPASSWORD", "")
PG_DATABASE = _env("PGDATABASE", "winserverrag")

# --- Indexing ---
INDEX_IMAGE_OCR = _env("INDEX_IMAGE_OCR", "0") == "1"
DRIVE_DOWNLOAD_TIMEOUT_SEC = _env_int("DRIVE_DOWNLOAD_TIMEOUT_SEC", 30)

# --- Embedding ---
EMBEDDING_MODEL = _env("EMBEDDING_MODEL", "paraphrase-multilingual-mpnet-base-v2")
EMBEDDING_DEVICE = _env("EMBEDDING_DEVICE", "auto")
CHUNK_SIZE = _env_int("CHUNK_SIZE", 600)
CHUNK_OVERLAP = _env_int("CHUNK_OVERLAP", 120)
BATCH_SIZE = _env_int("BATCH_SIZE", 64)
EMBEDDING_DIM = 768  # multi-qa-mpnet & paraphrase-multilingual-mpnet

# Reranker (cross-encoder) — lifts search quality after cosine retrieval.
# 0 = disabled, 1 = enabled. Pulls the model on first search; ~500MB.
ENABLE_RERANKER = _env("ENABLE_RERANKER", "1") == "1"
RERANKER_MODEL = _env("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
# How many candidates to retrieve from vector search before reranking.
# Larger = better recall, more compute. 50 is a sane default.
RERANKER_CANDIDATE_K = _env_int("RERANKER_CANDIDATE_K", 50)

# --- API retry ---
API_MAX_RETRIES = _env_int("API_MAX_RETRIES", 6)
API_BASE_DELAY_SEC = _env_float("API_BASE_DELAY_SEC", 5.0)
API_SHEET_MAX_RETRIES = _env_int("API_SHEET_MAX_RETRIES", 6)

# --- Daemon ---
DAEMON_ROTATE_INTERVAL_SEC = _env_int("DAEMON_ROTATE_INTERVAL_SEC", 300)
DAEMON_MIN_FREE_BYTES = _env_int("DAEMON_MIN_FREE_BYTES", 1_073_741_824)
# Disk-space auto-pause: stop new builds when free% < LOW, resume when free% >= RESUME.
# Hysteresis (LOW < RESUME) prevents rapid oscillation near the boundary.
DAEMON_DISK_LOW_PCT    = _env_float("DAEMON_DISK_LOW_PCT",    10.0)
DAEMON_DISK_RESUME_PCT = _env_float("DAEMON_DISK_RESUME_PCT", 20.0)
# Drive letter / path to check (default: drive where the project lives).
DAEMON_DISK_CHECK_PATH = _env("DAEMON_DISK_CHECK_PATH", "")
# Parallel worker threads (each builds/syncs one FD at a time).
# 4 is a reasonable default on a laptop; increase to saturate CPU for CPU-bound
# embedding, decrease if Google Drive / Sheets quota errors appear.
DAEMON_WORKER_THREADS = _env_int("DAEMON_WORKER_THREADS", 4)

# --- Control API ---
API_HOST = _env("API_HOST", "127.0.0.1")
API_PORT = _env_int("API_PORT", 17600)
API_BEARER_TOKEN = _env("API_BEARER_TOKEN", "")

# --- Derived paths ---
# In production these resolve under %ProgramData%\WinServerRAG\
# (created by the installer with users-modify perms). In dev they
# resolve next to the repo. parents=True so a fresh ProgramData layout
# without a pre-created run\ directory still works.
LOGS_DIR = DATA_ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
LOCK_DIR = DATA_ROOT / "run"
LOCK_DIR.mkdir(parents=True, exist_ok=True)
