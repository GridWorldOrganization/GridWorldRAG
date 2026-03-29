# VectorDB 設計メモ

## 参考リポジトリ

- **google-drive-agentic-rag** (donat-konan33)
  - https://github.com/donat-konan33/google-drive-agentic-rag
  - Google Drive 自動連携の実装を参考にする（認証・ドキュメント取り込みのみ）

### 参考リポジトリについての補足メモ

- Vector DB は **ChromaDB**（`./chroma_db/` にローカル保存）。GridWorldRAG では pgvector を採用するため不採用。
- Google Drive 本文はメモリ内処理のみ。ディスクには埋め込みとメタデータのみ保存。
- LLM（Claude / Mistral / Vertex AI）による回答生成・Gradio UI・FastAPI を含む完結した RAG システム。
- GridWorldRAG が参考にするのは **OAuth2 認証と GoogleDriveLoader の部分のみ**。LLM 回答生成は Claude Code（MCP 経由）が担うため、それ以外は不採用。

### なぜそのまま使わないか

- **Vector DB が ChromaDB**：SQL・複合フィルタ・将来の構造化データ連携を見据えて pgvector を採用するため不適。
- **LLM・UI が内蔵**：GridWorldRAG は Claude Code が MCP 経由でアクセスする設計のため、Gradio UI・FastAPI・LLM 回答生成はすべて不要な機能。
- **用途が違う**：参考リポは「ブラウザから人間が質問する RAG」。GridWorldRAG は「Claude Code がプログラム的に参照するナレッジベース」。

---

## Vector DB 選定経緯

### 検討した選択肢

| | ChromaDB | **PostgreSQL + pgvector（採用）** |
|---|---|---|
| 正体 | Vector DB 専用 | リレーショナル DB + ベクトル検索拡張 |
| セットアップ | `pip install` のみ | Homebrew で PostgreSQL + 拡張インストール |
| ベクトル検索 | ◎ 専用設計 | ○ 十分な性能 |
| メタデータフィルタ | △ 限定的 | ◎ SQL で自由に |
| 全文検索との併用 | ✗ | ◎ tsvector と組み合わせ可 |
| 構造化データとの共存 | ✗ | ◎ 同じ DB に混在可 |
| 本番運用実績 | △ | ◎ |

### 採用理由：PostgreSQL + pgvector

ベクトル検索を中心に使いつつ、SQL 全般の検索機能も持たせることで**長期的に柔軟な運用**を目的とする。

- Google Drive ドキュメントはオーナー・更新日時・種別などリッチなメタデータを持つため、SQL による複合フィルタリングが有効
- 将来的に構造化データ（スプレッドシート等）との JOIN も可能
- ChromaDB はベクトル検索専用で柔軟性に欠けるため不採用

```sql
-- pgvector を使った複合クエリの例
SELECT id, title, content, owner, updated_at
FROM documents
WHERE owner = 'tobisako@gridworld.co'
  AND updated_at > '2025-01-01'
ORDER BY embedding <=> $1  -- ベクトル類似度順
LIMIT 5;
```

---

## 開発環境

### 言語・ツール

- **Python 3.12+**
- **venv**（仮想環境）

### venv セットアップ

```bash
cd GridWorldRAG

# 仮想環境を作成（初回のみ）
python -m venv .venv

# 有効化（作業開始時に毎回実行）
source .venv/bin/activate

# パッケージインストール
pip install -r requirements.txt

# 依存関係を記録（パッケージ追加時）
pip freeze > requirements.txt
```

- `.venv/` は `.gitignore` に追加し Git 管理しない
- `requirements.txt` のみ Git 管理する

---

## システム構成・起動フロー

### 全体像

```
[初回のみ]
build_index.py
  └─ Google Drive 全件取得 → 埋め込み生成 → pgvector に一括投入
     （ファイル数によっては数時間かかる）

[常駐デーモン]
watcher.py
  ├─ Webhook 受信（リアルタイム）→ 変更ファイルを即 DB 反映
  └─ ポーリング（30〜60分おき）→ Webhook 漏れをキャッチ

[MCP サーバー]
mcp_server.py
  └─ Claude Code からの検索リクエスト → pgvector に問い合わせ → 結果返却
```

### 起動手順

```bash
# 1. 初回のみ：全件インデックス構築
python build_index.py

# 2. 常駐デーモン起動（以降ずっと起動しておく）
python watcher.py

# 3. MCP サーバー起動（Claude Code から使えるようにする）
python mcp_server.py
```

---

## Google Drive 変更監視：ハイブリッド方式

### 方針

Webhook（リアルタイム）とポーリング（安全網）を併用する。

| 方式 | 役割 | 頻度 |
|---|---|---|
| **Webhook** | リアルタイム検知・即時反映 | 変更発生時 |
| **ポーリング** | Webhook 漏れのキャッチ | 30〜60分おき |

### なぜポーリングも必要か

- Google Drive の Webhook（Push通知）は**配信を保証しない**（公式仕様）
- Mac スリープ中は通知を受け取れず漏れる
- Webhook チャンネルは**最大7日で失効**→ 再登録が必要

### ポーリングのロジック（Changes API）

「全ファイルをスキャン」はしない。**変更トークン**で差分のみ取得する：

```python
# 初回：開始トークンを取得・保存
response = drive_service.changes().getStartPageToken().execute()
save_token(response['startPageToken'])

# ポーリング時：前回トークン以降の変更のみ取得
page_token = load_token()
response = drive_service.changes().list(
    pageToken=page_token,
    spaces='drive',
).execute()

for change in response.get('changes', []):
    file_id = change['fileId']
    update_document_in_db(file_id)  # 変更分だけ DB 反映

# トークンを更新して保存
save_token(response.get('newStartPageToken'))
```

→ 100万ファイルあっても**変更があったファイルだけ**返るため軽量

### Webhook（ngrok でローカル Mac に公開URL付与）

```bash
# ngrok で Mac の 8507 ポートを公開
ngrok http 8507
# → https://xxxx.ngrok.io が発行される

# Google Drive に Webhook チャンネルを登録
drive_service.files().watch(
    fileId='root',
    body={
        'id': 'channel-001',
        'type': 'web_hook',
        'address': 'https://xxxx.ngrok.io/webhook',
        'expiration': ...,  # 最大7日、定期的に再登録が必要
    }
).execute()
```

---

## PostgreSQL + pgvector セットアップ（Mac）

```bash
# インストール
brew install postgresql@16
brew services start postgresql@16

# pgvector 拡張を有効化
psql -U postgres -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

### テーブル設計

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE documents (
    id          SERIAL PRIMARY KEY,
    title       TEXT,
    content     TEXT,
    owner       TEXT,
    source_url  TEXT,
    file_type   TEXT,
    updated_at  TIMESTAMPTZ,
    embedding   VECTOR(768),   -- sentence-transformers の次元数
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ベクトル検索用インデックス（IVFFlat）
CREATE INDEX ON documents USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- メタデータ検索用インデックス
CREATE INDEX ON documents (owner);
CREATE INDEX ON documents (updated_at);
```

---

## Google Drive 自動連携（参考実装からの知見）

### 認証方式

- Google OAuth2 を使用（スコープ: `drive.readonly`）
- `credentials.json`（Google Cloud Console から取得）をローカルに配置
- 初回認証後、`token.json` にトークンをキャッシュ → 以降は自動認証
- OAuth ローカルサーバーがポート **8506** で起動して認証フローを処理

```python
from langchain_google_community import GoogleDriveLoader

loader = GoogleDriveLoader(
    folder_id="<GOOGLE_DRIVE_FOLDER_ID>",
    token_path="./token.json",
    credentials_path="./credentials.json",
    recursive=True,
    num_results=15,
    file_types=["pdf", "presentation"],
)
documents = loader.load()
```

### セキュリティモデル

- `drive.readonly` スコープのみ → ファイルの作成・変更・削除は不可
- ファイル内容はメモリ内で処理、ローカルには埋め込みとメタデータのみ保存

### 必要な Google Cloud 設定手順

1. Google Cloud Project を作成
2. Google Drive API を有効化
3. OAuth 同意画面を設定（テストユーザーに自分のメールを追加）
4. OAuth 2.0 クライアント ID を作成 → `credentials.json` をダウンロード

---

## ドキュメント処理

### テキスト分割

```python
from langchain.text_splitter import RecursiveCharacterTextSplitter

splitter = RecursiveCharacterTextSplitter(
    chunk_size=600,    # 1チャンクあたり600文字
    chunk_overlap=120, # 前後20%オーバーラップでコンテキスト損失を防ぐ
)
chunks = splitter.split_documents(documents)
```

### 埋め込みモデル

- `sentence-transformers/multi-qa-mpnet-base-dot-v1` を使用（次元数: 768）
- ローカルで動作（API 不要、オフライン対応）

```python
from sentence_transformers import SentenceTransformer

model = SentenceTransformer("multi-qa-mpnet-base-dot-v1")
embeddings = model.encode([chunk.page_content for chunk in chunks])
```

---

## Google Workspace へのアクセス方法

### 完成コード（Python）

完成する Python コード（build_index.py / watcher.py 等）は **`google-api-python-client`** で Google Drive API を直接呼ぶ。MCP は一切使用しない。

- 参考：Google Workspace CLI — https://github.com/googleworkspace/cli
- Python ライブラリ：`google-api-python-client` + `google-auth-oauthlib`
- Google Cloud プロジェクト「claude code gcloud cli」（ID: `claude-code-gcloud-cli`）の OAuth 認証情報を使用

### workspace-mcp（開発時の確認用のみ）

- **workspace-mcp** (taylorwilsdon/google_workspace_mcp)
  - https://github.com/taylorwilsdon/google_workspace_mcp
  - Claude Code が開発中に Google Drive の内容を確認・検証する目的でのみ使用する
  - 完成コードには含めない

---

## GridWorldRAG での方針まとめ

| 項目 | 方針 |
|---|---|
| 言語 | Python 3.12+ |
| 仮想環境 | venv + requirements.txt |
| Vector DB | PostgreSQL + pgvector（Mac ローカル） |
| Google Drive 連携 | `google-api-python-client` で Drive API を直接呼ぶ |
| 変更監視 | Webhook（リアルタイム）+ ポーリング30〜60分（安全網） |
| Embeddings | sentence-transformers `multi-qa-mpnet-base-dot-v1`（ローカル動作） |
| LLM | Claude API（Anthropic） |
| MCP 連携 | pgvector を MCP サーバー経由で Claude Code に公開 |
| UI | なし（Claude Code から直接 MCP ツールで操作） |
