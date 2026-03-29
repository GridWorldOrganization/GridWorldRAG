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
    sheet_gid         TEXT,
    sheet_name        TEXT,
    permissions       JSONB,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

-- 既存 DB マイグレーション用
ALTER TABLE documents ADD COLUMN IF NOT EXISTS sheet_gid TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS sheet_name TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS permissions JSONB;

-- ベクトル検索用インデックス（IVFFlat）
CREATE INDEX IF NOT EXISTS idx_documents_embedding
    ON documents USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- メタデータ検索用インデックス
CREATE INDEX IF NOT EXISTS idx_documents_drive_file_id ON documents (drive_file_id);
CREATE INDEX IF NOT EXISTS idx_documents_owner ON documents (owner);
CREATE INDEX IF NOT EXISTS idx_documents_modified ON documents (drive_modified_at);
CREATE INDEX IF NOT EXISTS idx_documents_sheet_gid ON documents (sheet_gid);
