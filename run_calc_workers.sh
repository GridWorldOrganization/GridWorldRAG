#!/bin/bash
# GridWorldRAG - 最適ワーカー数の計算 → config.env に自動設定
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

source .venv/bin/activate
python calc_workers.py "$@"
