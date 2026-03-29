"""config.env の読み込みと設定値の管理。"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent


def load_config():
    """config.env を読み込んで環境変数にセットする。"""
    config_path = PROJECT_ROOT / "config.env"
    if not config_path.exists():
        print(f"エラー: {config_path} が見つかりません。")
        print("config.env.example をコピーして config.env を作成してください。")
        sys.exit(1)
    with open(config_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


if os.environ.get("GRIDWORLDRAG_SKIP_CONFIG") != "1":
    load_config()

    # 必須環境変数のバリデーション
    _REQUIRED = ["GOOGLE_EMAIL", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"]
    _missing = [v for v in _REQUIRED if v not in os.environ]
    if _missing:
        print(f"エラー: config.env に以下の値がありません: {', '.join(_missing)}")
        sys.exit(1)

# Google OAuth
GOOGLE_EMAIL = os.environ.get("GOOGLE_EMAIL", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")

# PostgreSQL
DB_NAME = os.environ.get("PGDATABASE", "gridworldrag")
DB_USER = os.environ.get("PGUSER", os.getenv("USER", ""))
DB_HOST = os.environ.get("PGHOST", "localhost")
DB_PORT = os.environ.get("PGPORT", "5432")

# インデックス対象スコープ
INDEX_MY_DRIVE = os.environ.get("INDEX_MY_DRIVE", "0") == "1"
INDEX_SHARED_DRIVES = os.environ.get("INDEX_SHARED_DRIVES", "1") == "1"
INDEX_IMAGE_OCR = os.environ.get("INDEX_IMAGE_OCR", "0") == "1"
PARALLEL_WORKERS = int(os.environ.get("PARALLEL_WORKERS", "8"))
TASK_SPLIT_THRESHOLD = int(os.environ.get("TASK_SPLIT_THRESHOLD", "5000"))
MONITOR_INTERVAL_MS = int(os.environ.get("MONITOR_INTERVAL_MS", "800"))
WORKER_START_INTERVAL_SEC = float(os.environ.get("WORKER_START_INTERVAL_SEC", "5"))

# 埋め込み
EMBEDDING_MODEL = "multi-qa-mpnet-base-dot-v1"
CHUNK_SIZE = 600
CHUNK_OVERLAP = 120
BATCH_SIZE = 100

# パス
TOKEN_PATH = PROJECT_ROOT / os.environ.get("GOOGLE_TOKEN_PATH", "token.pickle")
SHARED_DRIVES_WHITELIST_PATH = PROJECT_ROOT / "shared_drives_whitelist.txt"


def load_shared_drives_whitelist():
    """shared_drives_whitelist.txt からドライブ ID のセットを返す。

    ファイルが存在しない場合は空セット（全共有ドライブ対象外）。
    """
    if not SHARED_DRIVES_WHITELIST_PATH.exists():
        return set()
    ids = set()
    with open(SHARED_DRIVES_WHITELIST_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            drive_id = line.split("\t")[0].split(" ")[0].strip()
            if drive_id:
                ids.add(drive_id)
    return ids
