"""config.env の読み込みと設定値の管理。"""

import os
import sys
from pathlib import Path


class WorkerStatus:
    """ワーカーの進捗 JSON に書き込む status 値の定数。

    monitor_render.py と build_parallel.py で共有する。
    JSON は文字列で保存されるため str サブクラスにしない（定数として参照するだけ）。

    一覧:
        LOADING            : モデル・認証の初期化中
        RUNNING            : 通常稼働中（ファイル処理中）
        READY              : タスク待ち（次タスクの開始前）
        RATE_LIMITED       : API レート制限で一時停止中
        RATE_LIMITED_RUNNING: 処理継続しつつレート制限待ち
        DONE               : 全担当タスク完了
    """
    LOADING             = "loading"
    RUNNING             = "running"
    READY               = "ready"
    RATE_LIMITED        = "rate_limited"
    RATE_LIMITED_RUNNING = "rate_limited_running"
    DONE                = "done"

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
# DB_INDEX で gridworldrag_0 / gridworldrag_1 ... を切り替える。
# PGDATABASE を直接指定した場合はそちらが優先される。
DB_INDEX = int(os.environ.get("GRIDWORLDRAG_DB_INDEX", "0"))
DB_NAME = os.environ.get("PGDATABASE", f"gridworldrag_{DB_INDEX}")
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
DRIVE_DOWNLOAD_TIMEOUT_SEC = int(os.environ.get("DRIVE_DOWNLOAD_TIMEOUT_SEC", "30"))
FETCH_THREADS = int(os.environ.get("FETCH_THREADS", "3"))

# 埋め込み
EMBEDDING_MODEL = "multi-qa-mpnet-base-dot-v1"
CHUNK_SIZE = 600
CHUNK_OVERLAP = 120
BATCH_SIZE = 100
# PyTorch デバイス: auto (デフォルト、MPS/CUDA 優先), cpu (強制 CPU, MPS クラッシュ回避), mps, cuda
# Apple Silicon + PyTorch MPS backend のメモリ破壊バグに遭遇した場合は cpu に設定:
#   EMBEDDING_DEVICE=cpu   # config.env
# 参考: https://github.com/pytorch/pytorch/issues?q=MPS+softmax
EMBEDDING_DEVICE = os.environ.get("EMBEDDING_DEVICE", "auto")

# Google API リトライ設定 (issue #5)
# _api_call_with_retry のデフォルト挙動を config.env から制御可能にする。
API_MAX_RETRIES = int(os.environ.get("API_MAX_RETRIES", "6"))
API_BASE_DELAY_SEC = float(os.environ.get("API_BASE_DELAY_SEC", "5"))
# シート値取得専用の retry 上限 (メタデータ取得より厳しく設定可能)
API_SHEET_MAX_RETRIES = int(os.environ.get("API_SHEET_MAX_RETRIES", "6"))

# Telegram 通知設定 (issue #7)
# retry_pending 数が閾値を超えたら Telegram に直接 HTTPS API で送信する
# 空文字なら通知無効
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_RETRY_PENDING_THRESHOLD = int(os.environ.get("TELEGRAM_RETRY_PENDING_THRESHOLD", "10"))
# 同じ閾値で連続通知しないためのクールダウン (秒)
TELEGRAM_NOTIFY_COOLDOWN_SEC = int(os.environ.get("TELEGRAM_NOTIFY_COOLDOWN_SEC", "3600"))

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
