# GridWorldRAG

Google Drive のドキュメントを PostgreSQL + pgvector にインデックスし、Claude Code から MCP 経由でセマンティック検索するための RAG システム。

## アーキテクチャ

```
Google Drive ──→ build_index.py ──→ PostgreSQL + pgvector
                                          ↑
                 watcher.py ──────────────┘ (差分更新)
                                          ↑
Claude Code ←→ mcp_server.py ────────────┘ (検索)
```

## 必要な環境

| コンポーネント | バージョン | インストール |
|---|---|---|
| macOS | Apple Silicon (M1/M2) | - |
| Homebrew (ARM) | `/opt/homebrew` | [brew.sh](https://brew.sh) |
| Python | 3.12+ | `brew install python@3.12` |
| PostgreSQL | 17+ | `brew install postgresql@17` |
| pgvector | 0.8+ | `brew install pgvector` |

> **重要**: Apple Silicon Mac では必ず ARM 版 Homebrew (`/opt/homebrew`) を使用してください。Intel 版 (`/usr/local`) では PyTorch の最新版が利用できません。

## セットアップ

### 1. リポジトリをクローン

```bash
git clone https://github.com/GridWorldOrganization/GridWorldRAG.git
cd GridWorldRAG
```

### 2. Google Cloud OAuth 認証情報を取得

1. [Google Cloud Console](https://console.cloud.google.com/) にアクセス
2. プロジェクトを選択（または作成）
3. **API とサービス** → **認証情報** → **認証情報を作成** → **OAuth クライアント ID**
4. アプリケーションの種類: **デスクトップ アプリ**
5. Client ID と Client Secret をメモ
6. **API とサービス** → **有効な API とサービス** で **Google Drive API** を有効化

### 3. セットアップスクリプトを実行

```bash
./setup.sh
```

### 4. 設定ファイルを編集

```bash
# config.env に OAuth 認証情報を設定
vi config.env
```

```env
GOOGLE_EMAIL=your-email@example.com
GOOGLE_OAUTH_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_OAUTH_CLIENT_SECRET=your-client-secret
```

### 5. インデックス構築

```bash
source .venv/bin/activate
python build_index.py
```

初回実行時にブラウザが開き、Google アカウントでの認証が求められます。

## プロジェクト構成

```
GridWorldRAG/
├── README.md                 ← このファイル
├── CLAUDE.md                 ← Claude Code 向け開発メモ
├── setup.sh                  ← セットアップスクリプト
├── schema.sql                ← PostgreSQL テーブル定義
├── config.env.example        ← 設定テンプレート（Git管理）
├── config.env                ← 実際の設定（Git管理外）
├── requirements.txt          ← Python 依存パッケージ
├── build_index.py            ← 全件インデックス構築
├── watcher.py                ← 変更監視デーモン（予定）
├── mcp_server.py             ← Claude Code 用 MCP サーバー（予定）
├── src/
│   ├── __init__.py
│   ├── config.py             ← 設定読み込み
│   ├── drive_client.py       ← Google Drive API クライアント
│   └── db.py                 ← PostgreSQL + pgvector 操作
└── VectorDB/
    └── vectordb.md           ← 設計メモ・選定経緯
```

## モジュール構成

### `src/drive_client.py`

Google Drive API への全アクセスを担当。他のスクリプトから import して使う。

```python
from src.drive_client import authenticate, list_all_files, extract_text

service = authenticate()
files = list_all_files(service)
text = extract_text(service, files[0])
```

**提供する関数:**

| 関数 | 用途 |
|---|---|
| `authenticate()` | OAuth 認証 → Drive API サービスオブジェクトを返す |
| `list_all_files(service)` | 全ファイルのメタデータ一覧を取得 |
| `extract_text(service, file_info)` | ファイルからテキストを抽出（Docs/Sheets/PDF 対応） |
| `get_changes_start_token(service)` | Changes API の開始トークンを取得 |
| `list_changes(service, page_token)` | 前回以降の変更ファイルを取得 |

### `src/db.py`

PostgreSQL + pgvector の操作を担当。

```python
from src.db import connect, insert_chunks, search_similar

conn = connect()
insert_chunks(conn, chunks_data)
results = search_similar(conn, query_embedding, n_results=5)
```

### `src/config.py`

`config.env` を読み込み、設定値を提供する。import するだけで自動的に環境変数がセットされる。

## 技術スタック

| 役割 | 技術 |
|---|---|
| 言語 | Python 3.12 |
| Vector DB | PostgreSQL 17 + pgvector 0.8 |
| Google Drive | `google-api-python-client`（API 直接呼び出し） |
| 埋め込み | `sentence-transformers` (`multi-qa-mpnet-base-dot-v1`, 768次元) |
| テキスト分割 | `langchain-text-splitters` |
| PDF 抽出 | `pypdf` |
| Claude Code 連携 | MCP サーバー（予定） |

## ライセンス

Private repository - GridWorld Organization
