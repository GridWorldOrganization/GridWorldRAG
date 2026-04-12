#!/bin/bash
# GridWorldRAG - インデックス構築 起動スクリプト (3フェーズ)
#
# Phase 1: ファイル一覧取得 (Google Drive API)
# Phase 2: タスク分解 (即完了)
# Phase 3: VectorDB 作成 (並列ワーカー + モニター)
#
# 使い方: ./run_build.sh [--db N]   例: ./run_build.sh --db 1 → gridworldrag_1 に構築
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# --db N オプションを受け取る
DB_OPT=""
DB_LABEL="0 (gridworldrag_0)"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --db) DB_OPT="--db $2"; DB_LABEL="$2 (gridworldrag_$2)"; shift 2 ;;
        *) shift ;;
    esac
done

PROGRESS_DIR="/tmp/gridworldrag_progress"
LOG_FILE="/tmp/gridworldrag_build.log"
FILELIST_PKL="/tmp/gridworldrag_filelist.pkl"
TASK_DATA_PKL="/tmp/gridworldrag_taskdata.pkl"

# venv
PYTHON="$SCRIPT_DIR/.venv/bin/python"

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
echo " DB: $DB_LABEL"
echo " ドライブ数: $TOTAL_DRIVES  ワーカー数: $WORKER_COUNT"
echo "=========================================="

# 開始時刻
date +%s > /tmp/gridworldrag_start_time

# Ctrl+C トラップ（全フェーズ共通）
BUILD_PID=""
cleanup() {
    echo ""
    echo "停止中..."
    if [ -n "$BUILD_PID" ]; then
        kill $BUILD_PID 2>/dev/null
        wait $BUILD_PID 2>/dev/null
    fi
    echo "停止しました"
    exit 0
}
trap cleanup INT TERM

# --- Resume 判定 ---
SKIP_PHASE1=false
if [ -f "$FILELIST_PKL" ]; then
    # stty を一時的に復元して read を使えるようにする
    echo ""
    echo "前回のファイル一覧 (filelist.pkl) が残っています。"
    echo "  Y: resume (Phase 1 スキップ、前回の一覧を再利用)"
    echo "  N: 最初から取得 (ホワイトリスト更新した場合はこちら)"
    echo ""
    read -r -p "resume しますか？ [Y/n]: " REPLY
    case "$REPLY" in
        [nN])
            echo "→ Phase 1 からやり直します"
            rm -f "$FILELIST_PKL" "$TASK_DATA_PKL"
            ;;
        *)
            echo "→ resume: Phase 1 をスキップします"
            SKIP_PHASE1=true
            ;;
    esac
fi

# クリーンアップ
rm -rf "$PROGRESS_DIR"
mkdir -p "$PROGRESS_DIR"
: > "$LOG_FILE"

# --- Phase 1: ファイル一覧取得 ---
if [ "$SKIP_PHASE1" = false ]; then
    $PYTHON build_parallel.py --fetch-only $DB_OPT 2>>"$LOG_FILE"
fi

# --- Phase 2: タスク分解 ---
$PYTHON build_parallel.py --split-only $DB_OPT 2>>"$LOG_FILE"

# --- Phase 3: VectorDB 作成 (バックグラウンド + モニター) ---
echo ""
echo "=========================================="
echo " Phase 3: VectorDB 作成"
echo "=========================================="

# resource_tracker のセマフォリーク偽陽性警告を抑制
PYTHONWARNINGS="ignore::UserWarning:multiprocessing.resource_tracker" \
  $PYTHON build_parallel.py --work-only $DB_OPT >> "$LOG_FILE" 2>&1 &
BUILD_PID=$!

# モニター
bash "$SCRIPT_DIR/monitor.sh" "$BUILD_PID" "$WORKER_COUNT" "$MONITOR_INTERVAL_MS"
cleanup
