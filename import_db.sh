#!/bin/bash
# import_db.sh - gzipダンプをDBに取り込む（大阪側で実行）
#
# 使い方:
#   ./import_db.sh gridworldrag_0_20260330_1200.sql.gz
#   ./import_db.sh gridworldrag_0_20260330_1200.sql.gz --db 0

set -e
export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"

DUMP_FILE="$1"
DB_INDEX=0

shift
while [[ $# -gt 0 ]]; do
    case $1 in
        --db) DB_INDEX="$2"; shift 2 ;;
        *) echo "不明なオプション: $1"; exit 1 ;;
    esac
done

if [[ -z "$DUMP_FILE" ]]; then
    echo "使い方: $0 <dump.sql.gz> [--db N]"
    exit 1
fi

if [[ ! -f "$DUMP_FILE" ]]; then
    echo "エラー: ファイルが見つかりません: $DUMP_FILE"
    exit 1
fi

DB_NAME="gridworldrag_${DB_INDEX}"

echo "ファイル: $DUMP_FILE"
echo "DB: $DB_NAME"

# DB が存在しない場合は作成
if ! psql -lqt | cut -d\| -f1 | grep -qw "$DB_NAME"; then
    echo "DB作成中: $DB_NAME"
    createdb "$DB_NAME"
fi

# pgvector 拡張（初回のみ）
psql -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS vector;" 2>/dev/null || true

echo "インポート中..."
gunzip -c "$DUMP_FILE" | psql -d "$DB_NAME"

echo "完了: $DB_NAME にインポートしました"
