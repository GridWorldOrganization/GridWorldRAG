#!/bin/bash
# GridWorldRAG - インデックス構築 起動スクリプト
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PROGRESS_DIR="/tmp/gridworldrag_progress"
LOG_FILE="/tmp/gridworldrag_build.log"

# クリーンアップ
rm -rf "$PROGRESS_DIR"
mkdir -p "$PROGRESS_DIR"
: > "$LOG_FILE"

# venv
source .venv/bin/activate

# 設定読み込み
WORKER_COUNT=$(grep '^PARALLEL_WORKERS=' config.env 2>/dev/null | cut -d= -f2)
WORKER_COUNT=${WORKER_COUNT:-8}
MONITOR_INTERVAL_MS=$(grep '^MONITOR_INTERVAL_MS=' config.env 2>/dev/null | cut -d= -f2)
MONITOR_INTERVAL_MS=${MONITOR_INTERVAL_MS:-1000}
TOTAL_DRIVES=$(grep -v '^\s*#' shared_drives_whitelist.txt 2>/dev/null | grep -v '^\s*$' | wc -l | tr -d ' ')
TOTAL_DRIVES=${TOTAL_DRIVES:-0}
if grep -q '^INDEX_MY_DRIVE=1' config.env 2>/dev/null; then
    TOTAL_DRIVES=$((TOTAL_DRIVES + 1))
fi

echo "=========================================="
echo " GridWorldRAG - インデックス構築"
echo " ドライブ数: $TOTAL_DRIVES  ワーカー数: $WORKER_COUNT"
echo "=========================================="

# 開始時刻
date +%s > /tmp/gridworldrag_start_time

# Phase 1: ファイル一覧取得（stdout は画面に直接表示される）
python build_parallel.py --fetch-only 2>>"$LOG_FILE"

echo ""

# Phase 2: ワーカー処理（バックグラウンド）
python build_parallel.py --work-only >> "$LOG_FILE" 2>&1 &
BUILD_PID=$!

# Ctrl+C トラップ
cleanup() {
    # カーソルを表示領域の下に移動
    printf "\033[${WORKER_COUNT}B\n\n"
    echo "停止中..."
    if [ -n "$BUILD_PID" ]; then
        kill $BUILD_PID 2>/dev/null
        wait $BUILD_PID 2>/dev/null
    fi
    echo "停止しました"
    exit 0
}
trap cleanup INT TERM

# モニター
bash "$SCRIPT_DIR/monitor.sh" "$BUILD_PID" "$WORKER_COUNT" "$MONITOR_INTERVAL_MS"
cleanup
