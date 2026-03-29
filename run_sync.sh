#!/bin/bash
# GridWorldRAG - 差分同期 起動スクリプト
#
# 使い方:
#   ./run_sync.sh              # 差分同期
#   ./run_sync.sh --init       # 変更追跡トークンを初期化（初回のみ）
#   ./run_sync.sh --db N       # DB番号指定

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
source .venv/bin/activate
python sync.py "$@"
