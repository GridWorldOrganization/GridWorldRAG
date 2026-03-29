#!/bin/bash
# gridworld-rag-mcp 起動スクリプト
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."
source .venv/bin/activate
python gridworld-rag-mcp/server.py
