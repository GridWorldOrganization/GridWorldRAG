# GridWorldRAG コンテナイメージ
#
# 用途: ローカル Homebrew 依存を避けて試したいユーザー向け
# 注意: v0.2.x では公式サポート外（v1.0.0 で正式対応予定）
#
# ビルド: docker build -t gridworldrag:latest .
# 実行:  docker-compose up（推奨、pgvector コンテナも同時起動）

FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/GridWorldOrganization/GridWorldRAG"
LABEL org.opencontainers.image.description="Google Drive × pgvector × MCP RAG"
LABEL org.opencontainers.image.licenses="MIT"

# システム依存（psycopg2 source build, tesseract OCR, libpq）
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libssl-dev \
        tesseract-ocr \
        tesseract-ocr-jpn \
        tesseract-ocr-eng \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依存先インストール（layer キャッシュ効率化）
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --no-binary :all: psycopg2==2.9.11 \
    && pip install --no-cache-dir -r requirements.txt

# アプリ本体
COPY . .

# 実行時の DB 接続先は環境変数で上書き想定
# DB_HOST=pg / DB_PORT=5432 / DB_NAME=gridworldrag_1 等
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GRIDWORLDRAG_SKIP_CONFIG=0

# デフォルトは MCP サーバー起動
CMD ["python", "gridworld-rag-mcp/server.py"]
