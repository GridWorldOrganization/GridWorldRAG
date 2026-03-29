#!/bin/bash
# export_db.sh - DBをgzipダンプしてZIPファイルを出力する
#
# 使い方:
#   ./export_db.sh              # gridworldrag_0 を /tmp に出力
#   ./export_db.sh --db 1       # gridworldrag_1 を使用
#   ./export_db.sh --out ~/Desktop  # 出力先指定

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"

DB_INDEX=0
OUT_DIR="/tmp"

while [[ $# -gt 0 ]]; do
    case $1 in
        --db) DB_INDEX="$2"; shift 2 ;;
        --out) OUT_DIR="$2"; shift 2 ;;
        *) echo "不明なオプション: $1"; exit 1 ;;
    esac
done

DB_NAME="gridworldrag_${DB_INDEX}"
DATE=$(date +%Y%m%d_%H%M)
OUT_FILE="${OUT_DIR}/${DB_NAME}_${DATE}.sql.gz"

echo "DB: $DB_NAME"
echo "出力先: $OUT_FILE"
echo "ダンプ中..."

pg_dump "$DB_NAME" | gzip > "$OUT_FILE"

SIZE=$(du -sh "$OUT_FILE" | cut -f1)
echo "完了: $OUT_FILE ($SIZE)"
