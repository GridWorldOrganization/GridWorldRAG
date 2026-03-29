# GridWorldRAG

Google Drive のドキュメントを PostgreSQL + pgvector にインデックスし、Claude Code から MCP 経由でセマンティック検索するための RAG システム。

## 類似プロジェクトとの比較

Google Drive + pgvector の RAG システムを目指した公開リポジトリは存在するが、このプロジェクトの全要件を満たすものは見当たらない（2026年3月時点）。

| リポジトリ | Stars | 合致する点 | 不足している点 |
|---|---|---|---|
| [getomnico/omni](https://github.com/getomnico/omni) | 632 | Drive + pgvector + 権限継承 | Rust 製フルアプリ、Sheets シート別処理なし、MCP 非対応 |
| [taylorwilsdon/google_workspace_mcp](https://github.com/taylorwilsdon/google_workspace_mcp) | 1,962 | Google Workspace MCP として最完全 | RAG / ベクトル検索なし |
| [nofilamer/RAG_GDRIVE_PGVector_FASTAPI](https://github.com/nofilamer/RAG_GDRIVE_PGVector_FASTAPI) | 0 | Drive + pgvector の構成が近い | 権限なし、Sheets API 未使用、MCP 非対応 |

このプロジェクト固有の組み合わせ:

- **Sheets API でシート別に全データ取得**（CSV export ではなく全シート対応）
- **共有ドライブのホワイトリスト制御**（対象ドライブを明示指定）
- **pgvector + FastMCP の組み合わせ**（Claude Code から直接検索可能）
- **権限を JSONB で chunk 単位に保持**（ファイル単位ではなく細粒度の権限管理）

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

### 5. 並列ワーカー数の設定

```bash
# 自動設定（CPU コア数・空きメモリ・共有ドライブ数から最適値を算出）
./run_calc_workers.sh

# 手動で調整する場合は config.env を直接編集
vi config.env
# PARALLEL_WORKERS=8
```

ビルド中に他の作業もしたい場合はワーカー数を減らす（例: 8）。
PC をフル活用する場合は `./run_calc_workers.sh` で最適値を自動設定。

`TASK_SPLIT_THRESHOLD`（デフォルト: 5000）を超えるファイル数の共有ドライブは自動分割され、複数ワーカーで並列処理される。ワーカーはタスクキュー方式で、手が空いたら次のタスクを取る。

### 6. インデックス構築

```bash
# 並列版（推奨・リアルタイムモニター付き）
./run_build.sh

# シングルプロセス版
./run_build_single.sh
```

初回実行時にブラウザが開き、Google アカウントでの認証が求められます。

実行の流れ:
1. **ファイル一覧取得**（2〜3分）: 全共有ドライブからファイルメタデータを収集
2. **並列処理開始**: ワーカーごとにドライブを分担し、テキスト抽出→埋め込み→DB投入を並列実行
3. **モニター表示**（`run_build.sh` の場合）: 2秒ごとに各ワーカーの進捗率・チャンク数を表示

## プロジェクト構成

```
GridWorldRAG/
├── README.md                      ← このファイル
├── QUICKSTART.md                  ← クイックスタートガイド
├── CHANGELOG.md                   ← 変更履歴
├── LICENSE                        ← MIT License
├── CONTRIBUTING.md                ← コントリビューションガイド
├── schema.sql                     ← PostgreSQL テーブル定義
├── requirements.txt               ← Python 依存パッケージ
├── config.env.example             ← 設定テンプレート（Git管理）
├── shared_drives_whitelist.txt.example
├── setup.sh                       ← 初回セットアップ
├── build_parallel.py              ← 全件インデックス構築（並列・推奨）
├── build_single.py                ← 全件インデックス構築（シングル）
├── sync.py                        ← 差分同期（Google Drive Changes API）
├── calc_workers.py                ← 最適並列ワーカー数の算出
├── ocr_scan.py                    ← 画像OCR後追いツール
├── export_db.sh                   ← DBダンプ出力
├── import_db.sh                   ← DBダンプ取り込み
├── run_build.sh                   ← 並列ビルド起動 + モニター
├── run_build_single.sh            ← シングルプロセスビルド
├── run_sync.sh                    ← 差分同期実行
├── run_calc_workers.sh            ← 最適並列数の計算
├── monitor.sh                     ← リアルタイムモニター
├── list_dbs.sh                    ← DB一覧・サイズ表示
├── src/
│   ├── config.py                  ← 設定読み込み・定数管理
│   ├── drive_client.py            ← Google Drive API クライアント
│   ├── db.py                      ← PostgreSQL + pgvector 操作
│   ├── indexer.py                 ← チャンク生成ヘルパー
│   └── monitor_render.py          ← モニター表示レンダリング
├── gridworld-rag-mcp/
│   ├── server.py                  ← MCP サーバー（search / lookup / stats 等）
│   └── run_mcp.sh                 ← MCP サーバー起動スクリプト
└── docs/
    ├── technical.md               ← 技術リファレンス
    └── vectordb.md                ← ベクトルDB設計メモ
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
| `attach_folder_paths(files, drive_name)` | ファイルリストからフォルダパスを解決（追加 API コールなし） |
| `extract_text(service, file_info)` | ファイルからテキストを抽出（Docs/PDF/画像OCR 対応） |
| `extract_spreadsheet_sheets(file_id)` | スプレッドシートの全シートをシート別に取得 |
| `get_changes_start_token(service)` | Changes API の開始トークンを取得 |
| `list_changes(service, page_token)` | 前回以降の変更ファイルを取得 |

### `src/db.py`

PostgreSQL + pgvector の操作を担当。

```python
from src.db import connect, insert_chunks, search_similar, lookup_by_url

conn = connect()
insert_chunks(conn, chunks_data)
results = search_similar(conn, query_embedding, n_results=5)
doc = lookup_by_url(conn, "https://docs.google.com/spreadsheets/d/.../edit?gid=123")
```

### `gridworld-rag-mcp/server.py`

Claude Code 用の MCP サーバー。FastMCP ベースで 5 ツールを提供する。

| ツール | 用途 |
|---|---|
| `search` | セマンティック検索（クエリに URL が含まれる場合は `lookup` と併用） |
| `lookup` | Google Drive/Docs の URL からファイル内容を直接取得 |
| `stats` | インデックス DB の統計（総件数・ファイルタイプ・オーナー別集計） |
| `folder_tree` | DB からフォルダ構成ツリーを再構築して表示 |
| `recent_changes` | 直近の差分同期（`sync.py`）で追加・更新・削除されたファイル一覧 |

```bash
# Claude Code への登録
claude mcp add gridworld-rag-mcp -- python gridworld-rag-mcp/server.py

# DB 番号を指定する場合
claude mcp add gridworld-rag-mcp -- python gridworld-rag-mcp/server.py --db 1
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
| 画像 OCR | `pytesseract` + Tesseract 5.5 (jpn+eng) |
| Claude Code 連携 | MCP サーバー（予定） |

## ファイル種別ごとの対応状況

| ファイル種別 | テキスト抽出 | 備考 |
|---|---|---|
| Google Docs | OK | テキストとしてエクスポート |
| Google Sheets | OK (全シート) | Sheets API でシート別取得、シート名も検索可能 |
| Google Slides | OK | テキストとしてエクスポート |
| テキスト系 (txt, csv, json 等) | OK | 直接ダウンロード |
| PDF (テキストベース) | OK | pypdf でテキスト抽出 |
| PDF (スキャン/画像) | メタデータのみ | OCR 非対応（意図的に対象外） |
| 画像 (JPEG, PNG 等) | OCR (デフォルト OFF) | `INDEX_IMAGE_OCR=1` で有効化、または `ocr_scan.py` で後追い |
| 動画 (mp4 等) | メタデータのみ | ファイル名で検索可能 |
| 音声 (mp3 等) | メタデータのみ | ファイル名で検索可能 |

### 画像 OCR の後追い実行

`build_single.py` では画像OCRはデフォルトスキップ。後から個別に実行可能:

```bash
# ファイル単位
python ocr_scan.py --file-id 1ABCxyz

# フォルダ単位（再帰的に画像を検索）
python ocr_scan.py --folder-id 0AFxyz

# URL でも指定可能
python ocr_scan.py --url "https://drive.google.com/drive/folders/0AFxyz"

# ドライラン（対象確認のみ）
python ocr_scan.py --folder-id 0AFxyz --dry-run
```

### スプレッドシートの検索

- 1ファイル内の全シートをシート別にDB格納
- 各チャンクにシート名（`[シート: シート名]`）を含むため、シート名でも検索可能
- URL に `gid=` パラメータがある場合、そのシートのチャンクを優先して返す

## PostgreSQL データディレクトリの確認

PostgreSQL の実データがどこに保存されているか確認するには:

```bash
export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
psql -d postgres -c "SHOW data_directory;"
```

Homebrew (ARM) でインストールした場合、通常は `/opt/homebrew/var/postgresql@17` になる。

## 別マシンへのDB転送

Google Drive へのアクセス権がない環境でも、ビルド済みのDBをエクスポート・インポートすることで利用できる。

### エクスポート（ビルド済みマシン側）

```bash
# gridworldrag_0 を /tmp にダンプ
./export_db.sh

# 出力先・DB番号を指定
./export_db.sh --db 1 --out ~/Desktop
# → ~/Desktop/gridworldrag_1_20260330_1200.sql.gz
```

生成された `.sql.gz` ファイルを任意の方法で転送する。

### インポート（受け取りマシン側）

#### 事前準備（初回のみ）

```bash
# 1. リポジトリをクローン
git clone https://github.com/GridWorldOrganization/GridWorldRAG.git
cd GridWorldRAG

# 2. Python 環境構築
/opt/homebrew/opt/python@3.12/bin/python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. config.env を作成（Google認証は不要、DB設定のみ）
cp config.env.example config.env
# GRIDWORLDRAG_SKIP_CONFIG=1 を設定するか、以下の最小構成で作成:
echo "GRIDWORLDRAG_SKIP_CONFIG=1" > config.env
```

#### DBインポート

```bash
./import_db.sh gridworldrag_0_20260330_1200.sql.gz
# DB番号を指定する場合
./import_db.sh gridworldrag_0_20260330_1200.sql.gz --db 0
```

インポート完了後、MCPサーバーを起動すれば検索が使える。

#### MCPサーバー起動

```bash
# Claude Code に MCP サーバーを登録（初回のみ）
claude mcp add gridworld-rag-mcp -- python gridworld-rag-mcp/server.py

# DB番号を指定する場合
claude mcp add gridworld-rag-mcp -- python gridworld-rag-mcp/server.py --db 0
```

> **注意**: インポート先では `build_parallel.py` や `sync.py` は不要。MCPサーバーのみ動かせばよい。

## ライセンス

[MIT License](LICENSE)
