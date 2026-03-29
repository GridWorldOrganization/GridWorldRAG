#!/bin/bash
# GridWorldRAG - シングルプロセスでインデックス構築
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

source .venv/bin/activate
python build_single.py "$@"
