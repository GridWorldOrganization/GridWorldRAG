#!/bin/bash
# GridWorldRAG - ローテーション型差分同期 起動スクリプト
#
# 使い方:
#   ./run_sync_rotate.sh              # 全ドライブをチェック
#   ./run_sync_rotate.sh --init       # 全ドライブのトークン初期化（初回のみ）
#   ./run_sync_rotate.sh --db N       # DB番号指定
#   ./run_sync_rotate.sh --drive ID   # 特定ドライブのみ
#
# launchd (LaunchAgent) から 5 分間隔で呼び出す想定。

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
# venv の activate はプロジェクト移動時に壊れるので .venv/bin/python を直叩き
PYTHON="$SCRIPT_DIR/.venv/bin/python"
exec "$PYTHON" sync_rotate.py "$@"
