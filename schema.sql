-- GridWorldRAG データベーススキーマ
-- 実行: psql -U $USER -d gridworldrag -f schema.sql

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id                SERIAL PRIMARY KEY,
    drive_file_id     TEXT UNIQUE NOT NULL,
    title             TEXT,
    content           TEXT,
    chunk_index       INTEGER,
    owner             TEXT,
    source_url        TEXT,
    file_type         TEXT,
    drive_modified_at TIMESTAMPTZ,
    embedding         VECTOR(768),
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

-- ベクトル検索用インデックス（IVFFlat）
CREATE INDEX IF NOT EXISTS idx_documents_embedding
    ON documents USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- メタデータ検索用インデックス
CREATE INDEX IF NOT EXISTS idx_documents_drive_file_id ON documents (drive_file_id);
CREATE INDEX IF NOT EXISTS idx_documents_owner ON documents (owner);
CREATE INDEX IF NOT EXISTS idx_documents_modified ON documents (drive_modified_at);
