#!/bin/bash
# GridWorldRAG セットアップスクリプト（Mac ARM64）
set -e

echo "=== GridWorldRAG セットアップ ==="

# 1. Python venv
echo "[1/4] Python 仮想環境を作成中..."
if [ ! -d ".venv" ]; then
    /opt/homebrew/opt/python@3.12/bin/python3.12 -m venv .venv
    echo "  .venv 作成完了"
else
    echo "  .venv は既に存在します"
fi

# 2. パッケージインストール
echo "[2/4] パッケージをインストール中..."
source .venv/bin/activate
pip install -r requirements.txt -q

# 3. PostgreSQL データベース
echo "[3/4] PostgreSQL データベースをセットアップ中..."
export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
if psql -U "$USER" -d postgres -lqt | cut -d \| -f 1 | grep -qw gridworldrag; then
    echo "  データベース gridworldrag は既に存在します"
else
    createdb -U "$USER" gridworldrag
    echo "  データベース gridworldrag を作成しました"
fi
psql -U "$USER" -d gridworldrag -f schema.sql -q

# 4. config.env
echo "[4/4] 設定ファイルを確認中..."
if [ ! -f "config.env" ]; then
    cp config.env.example config.env
    echo "  config.env を作成しました。値を設定してください。"
else
    echo "  config.env は既に存在します"
fi

echo ""
echo "=== セットアップ完了 ==="
echo "次のステップ:"
echo "  1. config.env を編集して Google OAuth 情報を設定"
echo "  2. source .venv/bin/activate"
echo "  3. python build_index.py"
