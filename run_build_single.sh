#!/bin/bash
# GridWorldRAG - シングルプロセスでインデックス構築
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# venv の activate はプロジェクト移動時に壊れるので .venv/bin/python を直叩き
PYTHON="$SCRIPT_DIR/.venv/bin/python"
exec "$PYTHON" build_single.py "$@"
