# GridWorldRAG

Google Drive のドキュメントを PostgreSQL + pgvector にインデックスし、Claude Code から MCP 経由でセマンティック検索するための RAG システム。

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
├── README.md                 ← このファイル
├── QUICKSTART.md             ← クイックスタートガイド
├── LICENSE                   ← MIT License
├── CONTRIBUTING.md           ← コントリビューションガイド
├── run_calc_workers.sh        ← 最適並列数の計算 → config.env
├── run_build.sh               ← 並列ビルド起動 + モニター
├── run_build_single.sh        ← シングルプロセスビルド
├── setup.sh                  ← セットアップスクリプト
├── schema.sql                ← PostgreSQL テーブル定義
├── config.env.example        ← 設定テンプレート（Git管理）
├── config.env                ← 実際の設定（Git管理外）
├── requirements.txt          ← Python 依存パッケージ
├── build_single.py            ← 全件インデックス構築（シングル）
├── build_parallel.py         ← 全件インデックス構築（並列）
├── calc_workers.py           ← 最適並列ワーカー数の算出
├── ocr_scan.py               ← 画像OCR後追いツール
├── watcher.py                ← 変更監視デーモン（予定）
├── mcp_server.py             ← Claude Code 用 MCP サーバー（予定）
├── src/
│   ├── __init__.py
│   ├── config.py             ← 設定読み込み
│   ├── drive_client.py       ← Google Drive API クライアント
│   ├── db.py                 ← PostgreSQL + pgvector 操作
│   └── indexer.py            ← インデックス構築の共通処理
└── docs/
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

## ライセンス

[MIT License](LICENSE)
