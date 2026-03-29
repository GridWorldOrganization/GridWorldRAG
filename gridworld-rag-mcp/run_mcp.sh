#!/bin/bash
# gridworld-rag-mcp 起動スクリプト
# 使い方: ./run_mcp.sh [--db N]   例: ./run_mcp.sh --db 1 → gridworldrag_1 に接続
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."
source .venv/bin/activate
python gridworld-rag-mcp/server.py "$@"
