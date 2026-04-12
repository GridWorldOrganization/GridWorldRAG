#!/bin/bash
# GridWorldRAG - リアルタイム進捗モニター
# 使い方: ./monitor.sh [BUILD_PID] [WORKER_COUNT] [MONITOR_INTERVAL_MS]

BUILD_PID="${1:-}"
WORKER_COUNT="${2:-10}"
MONITOR_INTERVAL_MS="${3:-800}"
SLEEP_SEC=$(echo "scale=3; $MONITOR_INTERVAL_MS / 1000" | bc)
PROGRESS_DIR="/tmp/gridworldrag_progress"
LOG_FILE="/tmp/gridworldrag_build.log"
START_TIME_FILE="/tmp/gridworldrag_start_time"

if [ -f "$START_TIME_FILE" ]; then
    START_EPOCH=$(cat "$START_TIME_FILE")
else
    START_EPOCH=$(date +%s)
fi

# Enter キー等のキー入力で画面がスクロールしヘッダ行が重複する問題 (issue #3) の対策:
# - stty -echo で入力を画面に反映しない
# - stty -icanon で 1 文字ずつ受け取り改行を溜めない
# - stty echo で元に戻すのは trap と exit 時
_saved_stty=""
if tty -s 2>/dev/null; then
    _saved_stty=$(stty -g 2>/dev/null || true)
    stty -echo -icanon 2>/dev/null || true
fi
_restore_stty() {
    if [ -n "$_saved_stty" ]; then
        stty "$_saved_stty" 2>/dev/null || true
    fi
}
trap _restore_stty EXIT INT TERM

# 前回印刷した行数を追跡（相対カーソルアップで巻き戻す）
# tput sc/rc はサブプロセスが ESC 7 を発行すると保存位置が上書きされ、
# 20-30% の確率で表示がスクロールしてしまう問題があった。
# 相対カーソルアップ方式なら外部状態に依存しないため安定する。
LAST_LINE_COUNT=0

while true; do
    # 経過時間
    NOW_EPOCH=$(date +%s)
    ELAPSED=$((NOW_EPOCH - START_EPOCH))
    ELAPSED_STR=$(printf "%d:%02d" "$((ELAPSED / 60))" "$((ELAPSED % 60))")

    # Python で表示テキストを生成
    OUTPUT=$(python3 -m src.monitor_render "$WORKER_COUNT" "$PROGRESS_DIR" "$ELAPSED_STR" 2>/dev/null)

    # 前回印刷した行数分だけカーソルを上に戻す
    if [ $LAST_LINE_COUNT -gt 0 ]; then
        printf "\033[${LAST_LINE_COUNT}A\r"
    fi

    LINE_COUNT=0
    while IFS= read -r line; do
        printf '\033[K%s\n' "$line"
        LINE_COUNT=$((LINE_COUNT + 1))
    done <<< "$OUTPUT"
    # 前回より行数が減った場合、余剰行をクリア
    EXTRA=$((LAST_LINE_COUNT - LINE_COUNT))
    while [ $EXTRA -gt 0 ]; do
        printf '\033[K\n'
        LINE_COUNT=$((LINE_COUNT + 1))
        EXTRA=$((EXTRA - 1))
    done
    LAST_LINE_COUNT=$LINE_COUNT

    # プロセス終了チェック
    if [ -n "$BUILD_PID" ] && ! kill -0 "$BUILD_PID" 2>/dev/null; then
        echo ""
        echo "Build complete!"
        tail -10 "$LOG_FILE"
        exit 0
    fi

    sleep "$SLEEP_SEC"
done
