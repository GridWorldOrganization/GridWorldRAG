# VectorDB 設計メモ

> このドキュメントは v0.1.0 設計時に書かれた Vector DB 選定メモを、現行実装（v0.2.1）に合わせて更新したもの。
> 具体的な技術選定の正式記録は [ADR](./adr/) を参照。

---

## 採用: PostgreSQL 17 + pgvector 0.8

GridWorldRAG は Google Drive ドキュメントのセマンティック検索バックエンドに
**PostgreSQL + pgvector** を採用している。

### 選定の観点

| | ChromaDB | **PostgreSQL + pgvector（採用）** |
|---|---|---|
| 正体 | Vector DB 専用 | リレーショナル DB + ベクトル検索拡張 |
| セットアップ | `pip install` のみ | Homebrew で PostgreSQL + 拡張インストール |
| ベクトル検索 | ◎ 専用設計 | ○ IVFFlat / HNSW 両対応 |
| メタデータフィルタ | △ 限定的 | ◎ SQL で自由に |
| 全文検索との併用 | ✗ | ◎ tsvector と組合せ可 |
| 構造化データとの共存 | ✗ | ◎ 同じ DB に混在可 |
| 本番運用実績 | △ | ◎ |
| Claude Code MCP からの接続 | 要別 adaptor | ◎ psycopg2 で即時 |

### 採用理由

Google Drive のドキュメントはオーナー・更新日時・フォルダパス・権限 (JSONB)
といった構造化メタデータを多く持つため、**ベクトル類似度 + メタデータフィルタ**
の複合クエリを SQL で表現できる pgvector が最適だった。

```sql
-- 例: 特定オーナーかつ直近1年の文書からセマンティック検索
SELECT id, title, content, owner, drive_modified_at
FROM documents
WHERE owner = 'tobisako@gridworld.co'
  AND drive_modified_at > NOW() - INTERVAL '1 year'
ORDER BY embedding <=> $1     -- cosine 距離
LIMIT 5;
```

詳細は [ADR 0001: pgvector を採用した理由](./adr/0001-pgvector-over-chroma.md) 参照。

---

## 現行スキーマ（v0.2.1）

`schema.sql` の正本は repo root にある。本ドキュメントでは設計上のポイントのみ抜粋。

### `documents` テーブル

| カラム | 型 | 説明 |
|--------|-----|------|
| `id` | SERIAL PK | 自動採番 |
| `drive_file_id` | TEXT UNIQUE | `{file_id}_chunk_{n}` または `{file_id}_sheet_{gid}_chunk_{n}` |
| `title` | TEXT | ファイル名 |
| `content` | TEXT | チャンクテキスト（NUL 文字除去済み） |
| `chunk_index` | INTEGER | チャンク番号 |
| `owner` | TEXT | オーナーのメールアドレス |
| `source_url` | TEXT | Google Drive の URL |
| `file_type` | TEXT | MIME タイプ |
| `drive_modified_at` | TIMESTAMPTZ | Drive 上の更新日時 |
| `embedding` | VECTOR(768) | 埋め込みベクトル（paraphrase-multilingual-mpnet-base-v2） |
| `sheet_gid` | TEXT | スプレッドシートのシート ID |
| `sheet_name` | TEXT | スプレッドシートのシート名 |
| `permissions` | JSONB | 権限リスト（下記） |
| `partial_content` | BOOLEAN | テキスト抽出が部分的な場合 TRUE |
| `folder_path` | TEXT | `Drive / Folder / Sub` |
| `created_at` | TIMESTAMPTZ | DB 投入日時 |

### `permissions` JSONB 構造

```json
[
  {"email": "user@example.com", "name": "田中", "role": "writer", "type": "user"},
  {"email": "example.com", "name": "", "role": "reader", "type": "domain"}
]
```

チャンク単位で保持することで、将来的に「閲覧者フィルタ付き検索」に発展できる（現在は記録のみ）。

### インデックス

```sql
-- ベクトル類似度（IVFFlat）
CREATE INDEX ON documents USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- メタデータ検索
CREATE INDEX ON documents (drive_file_id);
CREATE INDEX ON documents (owner);
CREATE INDEX ON documents (drive_modified_at);
```

---

## 埋め込みモデル

### 現行: `paraphrase-multilingual-mpnet-base-v2`（v0.2.1〜）

- **次元数**: 768
- **Tokenizer**: SentencePiece（多言語対応）
- **対応言語**: 50+ 言語
- **ローカル実行**: CPU/GPU（Metal, CUDA）で動作、API 不要

#### なぜ多言語モデルか（重要）

v0.2.0 以前は `multi-qa-mpnet-base-dot-v1` を使用していたが、これは **英語 tokenizer 専用**
だったため、日本語の熟語漢字を全て `[UNK]`（id=104）に潰していた。結果:

- 「採用計画」「税務申告」のような短い日本語クエリが同一 embedding になる
- cosine 1.00 の誤ヒット多発（100 クエリ中 17 件が dist=0.000）

`paraphrase-multilingual-mpnet-base-v2` に切替後:
- 「採用計画 vs 税務申告」cosine 1.00 → 0.36
- 100 クエリ中 dist=0.000 が 17件 → 0件
- 768 次元維持のため schema 変更不要

詳細は [CHANGELOG v0.2.1](../CHANGELOG.md#021---2026-04-21) 参照。

### モデル切替時の運用

768 次元を維持する範囲内であれば schema 変更は不要だが、**ベクトルの意味空間が変わるため
既存 `embedding` 列は再計算が必須**。DB TRUNCATE → 全件再ビルドで対応する。

```bash
# 再ビルド手順
psql -d gridworldrag_1 -c "TRUNCATE documents;"
./run_build.sh
```

---

## テキスト分割

```python
from langchain_text_splitters import RecursiveCharacterTextSplitter

splitter = RecursiveCharacterTextSplitter(
    chunk_size=600,    # 1チャンクあたり 600 文字
    chunk_overlap=120, # 20% オーバーラップ（文脈損失防止）
)
```

分割は `src/indexer.py` のヘルパー経由で実施。スプレッドシートはシート単位で
先に分割してからテキストチャンク化する（シート名を検索ヒット対象にするため）。

---

## 検索のフロー

```
[クエリ文字列]
    │
    ├── URL 含む? ── Yes ──→ lookup_by_url() で直接取得
    │
    └── No ──→ SentenceTransformer.encode() → vector
                              │
                              ▼
                  SELECT ... ORDER BY embedding <=> $1 LIMIT N
                              │
                              ▼
                   [結果: title, content, url, score]
```

実装は `src/db.py` の `search_similar()` と `lookup_by_url()`、
および `gridworld-rag-mcp/server.py` の `search` / `lookup` ツール。

---

## データフロー全体

```
Google Drive
    │
    ├── 初回: build_parallel.py (3 フェーズ並列)
    │       ├── Phase 1: ファイル一覧取得（FETCH_THREADS=3）
    │       ├── Phase 2: タスク分割（TASK_SPLIT_THRESHOLD）
    │       └── Phase 3: テキスト抽出 → チャンク → embedding → INSERT
    │
    └── 差分: sync_rotate.py（launchd/Task Scheduler から 5 分間隔）
            └── Changes API (drive 単位) → 変更ファイルだけ upsert

             ↓ すべて UPSERT（ON CONFLICT DO UPDATE）

[PostgreSQL 17 + pgvector]

             ↑ SELECT ... ORDER BY embedding <=> $1

Claude Code ←→ gridworld-rag-mcp/server.py (FastMCP)
                └── ツール: search, lookup, stats,
                            folder_tree, recent_changes, sync_history
```

詳細は [architecture.md](./architecture.md) 参照。

---

## 運用上の注意

### `.venv/` は git 管理外

- `.gitignore` で除外済
- `requirements.txt` のみ管理

### Homebrew ARM 必須（Mac）

Apple Silicon Mac では `/opt/homebrew` の ARM 版 Python / PostgreSQL を使うこと。
Intel 版 (`/usr/local`) では PyTorch 最新版が入らない。詳細は [README.md](../README.md) 冒頭参照。

### `psycopg2` は source build 版

`psycopg2-binary`（バンドル版）は libssl を同梱するため、Python の `_ssl` モジュールと
二重の OpenSSL がプロセス内に同居し SSL 不安定化を起こす。
`requirements.txt` では `psycopg2` を指定し、システムの OpenSSL を共有する。

詳細は [ADR 0003: psycopg2 source build](./adr/0003-psycopg2-source-over-binary.md) 参照。

---

## 過去の検討記録（歴史的記述）

### `google-drive-agentic-rag`（参考のみ）

- https://github.com/donat-konan33/google-drive-agentic-rag
- OAuth2 認証と GoogleDriveLoader の部分を参考にした
- Vector DB は ChromaDB、LLM 回答生成 (Claude / Mistral / Vertex AI) と Gradio UI を内蔵
- **不採用**: GridWorldRAG は Claude Code が MCP 経由でアクセスする設計のため、LLM・UI は不要

### Webhook + ポーリング併用案（v0.1.x 検討、不採用）

v0.1.x の初期設計では Google Drive の **Push Notification (Webhook)** を
ngrok 経由で受ける案も検討したが、以下の理由で採用されず、現在の **Changes API ポーリング方式**
（`sync_rotate.py`）に落ち着いた:

- Webhook は配信保証がない（Google の公式仕様）
- Mac スリープ中は受信不能
- Webhook チャンネルは最大7日で失効 → 再登録運用が面倒
- ngrok 依存は本番運用に適さない

Changes API は drive 単位でページトークンを持ち、変更ゼロなら 1 API コールで即終了するため、
5 分間隔で 22 ドライブを一巡しても 7 秒で完了する。Webhook 無しでも実用的なリアルタイム性を
得られると判断した。
