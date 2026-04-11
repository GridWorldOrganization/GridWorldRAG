#!/bin/bash
# GridWorldRAG - 最適ワーカー数の計算 → config.env に自動設定
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# venv の activate はプロジェクト移動時に壊れるので .venv/bin/python を直叩き
PYTHON="$SCRIPT_DIR/.venv/bin/python"
exec "$PYTHON" calc_workers.py "$@"
