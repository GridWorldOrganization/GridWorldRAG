#!/bin/bash
# GridWorldRAG - DBリスト表示
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
# venv の activate はプロジェクト移動時に壊れるので .venv/bin/python を直叩き
PYTHON="$SCRIPT_DIR/.venv/bin/python"

"$PYTHON" - <<'EOF'
import os, sys
sys.path.insert(0, '.')
os.environ['GRIDWORLDRAG_SKIP_CONFIG'] = '1'

import psycopg2
from pathlib import Path

# config.env から DB_NAMES を読み込む（例: DB_NAMES=0:test1,1:本番）
db_names = {}
config_path = Path('config.env')
if config_path.exists():
    for line in config_path.read_text().splitlines():
        line = line.strip()
        if line.startswith('DB_NAMES='):
            for entry in line[len('DB_NAMES='):].split(','):
                entry = entry.strip()
                if ':' in entry:
                    idx, name = entry.split(':', 1)
                    db_names[idx.strip()] = name.strip()
        if line.startswith('PGUSER='):
            os.environ.setdefault('PGUSER', line[len('PGUSER='):].strip())

pguser = os.environ.get('PGUSER', os.getenv('USER', ''))

try:
    conn = psycopg2.connect(dbname='postgres', user=pguser, host='localhost')
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("""
        SELECT datname, pg_size_pretty(pg_database_size(datname))
        FROM pg_database
        WHERE datname LIKE 'gridworldrag_%'
        ORDER BY datname
    """)
    dbs = cur.fetchall()
    cur.close()
    conn.close()
except Exception as e:
    print(f"PostgreSQL接続エラー: {e}")
    sys.exit(1)

if not dbs:
    print("gridworldrag_* DBが見つかりません。")
    sys.exit(0)

from src.db import connect

print(f"{'#':>3}  {'DB名':<20} {'名称':<12} {'サイズ':>8}  {'チャンク数':>10}  {'ファイル数':>8}  {'最終インデックス'}")
print("-" * 90)

for dbname, size in dbs:
    idx = dbname.replace('gridworldrag_', '')
    label = db_names.get(idx, '-')
    try:
        conn2 = connect(db_name=dbname)
        cur2 = conn2.cursor()
        cur2.execute("SELECT COUNT(*) FROM documents")
        chunks = cur2.fetchone()[0]
        cur2.execute("SELECT COUNT(DISTINCT title) FROM documents")
        files = cur2.fetchone()[0]
        cur2.execute("SELECT MAX(created_at) FROM documents")
        row = cur2.fetchone()
        last_update = row[0].strftime('%Y-%m-%d %H:%M') if row and row[0] else '-'
        cur2.close()
        conn2.close()
        print(f"{idx:>3}  {dbname:<20} {label:<12} {size:>8}  {chunks:>10,}  {files:>8,}  {last_update}")
    except Exception as e:
        print(f"{idx:>3}  {dbname:<20} {label:<12} {size:>8}  {'(エラー: ' + str(e)[:30] + ')'}")
EOF
