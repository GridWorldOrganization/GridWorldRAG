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


load_config()

# Google OAuth
GOOGLE_EMAIL = os.environ["GOOGLE_EMAIL"]
GOOGLE_CLIENT_ID = os.environ["GOOGLE_OAUTH_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_OAUTH_CLIENT_SECRET"]

# PostgreSQL
DB_NAME = os.environ.get("PGDATABASE", "gridworldrag")
DB_USER = os.environ.get("PGUSER", os.getenv("USER", "tobisako"))
DB_HOST = os.environ.get("PGHOST", "localhost")
DB_PORT = os.environ.get("PGPORT", "5432")

# 埋め込み
EMBEDDING_MODEL = "multi-qa-mpnet-base-dot-v1"
CHUNK_SIZE = 600
CHUNK_OVERLAP = 120
BATCH_SIZE = 100

# パス
TOKEN_PATH = PROJECT_ROOT / os.environ.get("GOOGLE_TOKEN_PATH", "token.pickle")
