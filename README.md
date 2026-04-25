<div align="center">

# GridWorldRAG

**Google Drive × pgvector × MCP — Claude Code 向けセマンティック検索バックエンド**

[![CI](https://github.com/GridWorldOrganization/GridWorldRAG/actions/workflows/lint.yml/badge.svg)](https://github.com/GridWorldOrganization/GridWorldRAG/actions/workflows/lint.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![PostgreSQL 17](https://img.shields.io/badge/PostgreSQL-17-336791.svg)](https://www.postgresql.org/)
[![pgvector 0.8+](https://img.shields.io/badge/pgvector-0.8%2B-4B8BBE.svg)](https://github.com/pgvector/pgvector)
[![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Windows%20WSL2%20%7C%20Linux-lightgrey.svg)](#対応-os)
[![Last Commit](https://img.shields.io/github/last-commit/GridWorldOrganization/GridWorldRAG.svg)](https://github.com/GridWorldOrganization/GridWorldRAG/commits/master)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](./CONTRIBUTING.md)

</div>

Google Drive のドキュメントを PostgreSQL + pgvector にインデックスし、Claude Code から MCP 経由でセマンティック検索するための RAG システム。

> **対象 OS**: Windows 11 ネイティブのみ。Mac 版（v0.2.1 まで）は開発中止し、本リポジトリは **Windows サービス常駐型 (`rag_daemon.py` + FastAPI + Electron ミニモニタ + NSSM)** に一本化されています。Mac 版時代の運用手順（launchd 等）はリポジトリ履歴・`docs/` の archive に残っていますが、現行サポート対象外です。

## 設計概要

本 RAG は **Windows サービス（デーモン）として常駐** し、以下を自動実行する:

1. **Google Drive 共有フォルダのスクリーニング** — ホワイトリスト指定の共有ドライブを Changes API で監視、追加/更新/削除ファイルを検出
2. **RAG 構築** — 検出ファイルからテキスト抽出 → チャンク分割 → 埋め込み生成 → PostgreSQL + pgvector へ UPSERT
3. **稼働状況の可視化** — **ミニモニタ**（Electron 製の小型ウィンドウ）でサービスの稼働状況・処理中ファイル・差分同期の最新結果をリアルタイム表示

```
┌────────────────────────────────────────────────────────────┐
│ Windows サービス常駐（NSSM 登録）                          │
│                                                            │
│  ┌───────────────┐    ┌──────────────┐    ┌────────────┐ │
│  │ Drive 監視     │ →  │ RAG 構築     │ →  │ pgvector   │ │
│  │ (Changes API) │    │ (抽出/埋込) │    │ (PG 17)    │ │
│  └───────┬───────┘    └──────┬───────┘    └─────┬──────┘ │
│          │                    │                  │         │
│          └────────────┬───────┴──────────────────┘         │
│                       ↓                                     │
│              ┌────────────────┐                            │
│              │ FastAPI 状態 API│                            │
│              └────────┬───────┘                            │
└───────────────────────┼─────────────────────────────────────┘
                        ↓
              ┌──────────────────┐
              │ ミニモニタ       │ ← ユーザー監視
              │ (Electron)       │
              └──────────────────┘
                        ↓
              ┌──────────────────┐
              │ Claude Code      │ ← MCP 経由 検索
              │ (gridworld-rag-mcp)│
              └──────────────────┘
```

**主要コンポーネント:**

| 要素 | 実装 | 役割 |
|---|---|---|
| サービス常駐 | NSSM (Non-Sucking Service Manager) | Windows サービスとして自動起動・再起動 |
| Drive 監視 | `drive_client.list_changes()` | 共有ドライブごとに独立トークン、変更ゼロなら即スキップ |
| RAG 構築 | `rag_daemon.py` + `sentence-transformers` | 768次元 multi-qa-mpnet-base-dot-v1 |
| 状態 API | FastAPI (localhost) | サービスの稼働状況・進捗を公開 |
| ミニモニタ | Electron 小型ウィンドウ | 稼働状況・処理中ファイル・最新差分を可視化 |
| 検索 IF | MCP (FastMCP) | Claude Code から `search` / `lookup` 等で検索 |

**運用ポリシー:**

- Windows サービスとして常駐（PC 起動時に自動開始、Task Scheduler は使わない）
- ミニモニタは任意起動。閉じてもサービスは動作継続
- 共有ドライブはホワイトリスト方式（`shared_drives_whitelist.txt`）
- 認証は OAuth デスクトップアプリ。token は secrets リポで管理

### サービス挙動仕様

本サービスは **バックグラウンドで RAG 構築を行う常駐プロセス** である。ミニモニタとは API 経由で双方向通信し、ユーザー操作で RAG ビルドの開始/停止を制御できる。

**ミニモニタ → サービス（制御）:**

- ミニモニタには **「開始」「停止」ボタン**（トグル式）を配置
- ボタン押下でサービスへ通知（HTTP POST）:
  - 「開始」 → `POST /api/daemon/resume` → サービスは新規タスクの取得を再開し RAG ビルドを進める
  - 「停止」 → `POST /api/daemon/pause` → サービスは新規タスクの取得を停止（in-flight タスクは完走）
- 停止状態は `daemon_config.paused` フラグとして DB に永続化、サービス再起動後も状態を保持

**サービス → ミニモニタ（状態確認）:**

- ミニモニタは `GET /api/stats`（既存の 250ms ポーリング）で **RAG ビルドの開始状態 / 停止状態** を取得
- レスポンスに `paused: bool` フィールドを追加
- ミニモニタは画面上に現在の状態を表示:
  - **稼働中** — 緑インジケータ + 「停止」ボタン表示
  - **停止中** — 灰インジケータ + 「開始」ボタン表示
- ステータス文言・進捗バーも停止中は明示（"停止中（手動停止）"）

**API エンドポイント一覧:**

| メソッド | パス | 用途 |
|---|---|---|
| `GET` | `/api/daemon/state` | 現在の稼働/停止状態を取得（`{paused: bool, since: ISO8601}`） |
| `POST` | `/api/daemon/pause` | RAG ビルド停止（新規タスク取得を止める） |
| `POST` | `/api/daemon/resume` | RAG ビルド開始（新規タスク取得を再開） |
| `GET` | `/api/stats` | 統計 + `paused` フラグ（ミニモニタが定期取得） |

**設計上の注意:**

- 「停止」は **ワーク一時停止**であり、Windows サービス自体の停止ではない。FastAPI は稼働継続するためミニモニタの API 接続は維持される
- 進行中タスクは強制中断せず完走させる（DB 整合性保護）
- 停止状態でも Drive Changes API のトークンは進めない（次回開始時に取りこぼしなく再開）
- サービス本体（NSSM 登録の Windows サービス）の停止/再起動は OS の `services.msc` または `nssm stop` から行う（ミニモニタの管轄外）

詳細実装は `C:\claude_code\dev\WinServerRAG\` 配下、設計背景は [docs/architecture.md](./docs/architecture.md) 参照。

<details>
<summary><b>📖 目次（クリックで展開）</b></summary>

- [類似プロジェクトとの比較](#類似プロジェクトとの比較)
- [デモ](#デモ)
- [必要な環境](#必要な環境)
  - [対応 OS](#対応-os)
  - [共通コンポーネント](#共通コンポーネント)
- [セットアップ](#セットアップ)
- [プロジェクト構成](#プロジェクト構成)
- [モジュール構成](#モジュール構成)
- [技術スタック](#技術スタック)
- [ファイル種別ごとの対応状況](#ファイル種別ごとの対応状況)
- [PostgreSQL データディレクトリの確認](#postgresql-データディレクトリの確認)
- [別マシンへの DB 転送](#別マシンへのdb転送)
- [差分同期の自動化](#差分同期の自動化)
  - [Mac 版（launchd）](#mac-版launchdの詳細)
  - [Windows 版（Task Scheduler + WSL2）](#windows-版task-scheduler--wsl2の詳細)
- [テスト](#テスト)
- [ドキュメント](#ドキュメント)
- [コントリビューション](#コントリビューション)
- [セキュリティ](#セキュリティ)
- [ロードマップ](#ロードマップ)
- [謝辞](#謝辞)
- [ライセンス](#ライセンス)

</details>

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

## デモ

### リアルタイムモニター（`./run_build.sh` 実行中）

<!-- docs/images/monitor.gif を配置後、下記コメントを解除 -->
<!-- ![Monitor demo](./docs/images/monitor.gif) -->

```
==================== GridWorldRAG Build Monitor ====================
 Drives: 22     Workers: 8     Tasks: 28/28    Elapsed: 12:34

 W1  GW_LIB_過去PJ実績(1/7)  ████████░░░░░░░░  48%  running
 W2  GW_PJ(2/3)              ██████░░░░░░░░░░  37%  running
 W3  GW_マーケティング        完了 (52 ファイル)            done
 W4  GW_営業                  ████░░░░░░░░░░░░  25%(レート制限待ち)
 ...

 処理済み: 12,340   スキップ: 34,002   エラー: 3   チャンク: 95,128
=====================================================================
```

### Claude Code からの検索例

<!-- docs/images/mcp_search.png を配置後、下記コメントを解除 -->
<!-- ![MCP search demo](./docs/images/mcp_search.png) -->

```
User: 2026年の採用計画の最新資料を教えて

Claude: [MCP: gridworld-rag-mcp.search 実行]
検索結果:
1. 2026採用計画_v3.gdoc
   (更新: 2026-04-15, オーナー: hr@gridworld.co)
   内容: 2026年度の採用目標は...

2. [シート: エンジニア職] 2026_採用進捗.gsheet
   (更新: 2026-04-20)
   内容: エンジニア採用は現在...

3. 採用戦略レビュー議事録_2026Q2.gdoc
   ...
```

### 30 秒で試せるクエリ例（MCP 接続後）

```
# セマンティック検索
/mcp gridworld-rag-mcp search "今四半期の売上目標"

# URL から直接ルックアップ
/mcp gridworld-rag-mcp lookup "https://docs.google.com/spreadsheets/d/.../edit?gid=123"

# フォルダツリー再構築
/mcp gridworld-rag-mcp folder_tree

# 直近の差分同期結果
/mcp gridworld-rag-mcp recent_changes

# DB 統計
/mcp gridworld-rag-mcp stats
```

## 必要な環境

### 対応 OS

| OS | サポート状況 | 常駐実行 |
|---|---|---|
| **macOS (Apple Silicon M1/M2/M3)** | ◎ フル対応 | launchd LaunchAgent |
| **macOS (Intel)** | △ 動作するが非推奨（PyTorch 最新が入らない） | launchd LaunchAgent |
| **Windows 10/11** | ◎ WSL2 経由で対応（v0.2.1〜） | Windows Task Scheduler |
| **Linux (Ubuntu 22.04+)** | ○ 動作確認済（常駐は手動 systemd 設定） | systemd（自前） |

### 共通コンポーネント

| コンポーネント | バージョン | Mac インストール | Windows (WSL) インストール |
|---|---|---|---|
| Homebrew (ARM) / apt | - | [brew.sh](https://brew.sh) | `apt-get` |
| Python | 3.12+ | `brew install python@3.12` | `apt install python3.12` |
| PostgreSQL | 17+ | `brew install postgresql@17` | `apt install postgresql-17` |
| pgvector | 0.8+ | `brew install pgvector` | [build from source](https://github.com/pgvector/pgvector#installation) |

> **重要 (Mac)**: Apple Silicon Mac では必ず ARM 版 Homebrew (`/opt/homebrew`) を使用してください。Intel 版 (`/usr/local`) では PyTorch の最新版が利用できません。
>
> **重要 (Windows)**: ネイティブ Windows では動作しません。**WSL2 (Ubuntu 22.04+) 必須**。Windows Task Scheduler から WSL 内の `run_sync_rotate.sh` を呼び出す構成です。詳細は [scheduler/windows/README.md](./scheduler/windows/README.md) 参照。

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

`FETCH_THREADS`（デフォルト: 3）はファイル一覧取得フェーズのスレッド数。共有ドライブごとに並列でメタデータを取得する。ワーカー数とは独立した設定で、フェッチフェーズ完了後に並列ワーカーが起動する。

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
1. **Phase 1: ファイル一覧取得**（2〜5分）: 全共有ドライブからファイルメタデータを収集
2. **Phase 2: タスク分解**（即完了）: ドライブを `TASK_SPLIT_THRESHOLD` 単位でタスクに分割
3. **Phase 3: VectorDB 作成**: ワーカーがタスクを分担し、テキスト抽出→埋め込み→DB投入を並列実行 + リアルタイムモニター

前回のファイル一覧（filelist.pkl）が残っている場合、起動時に resume プロンプトが表示される:
- **Y**（デフォルト、Enter のみ）: Phase 1 スキップ、前回の一覧を再利用
- **N**: ホワイトリスト更新時はこちら。最初から取得し直す

#### 中断と再開（resume）

ビルドはいつでも Ctrl+C で安全に中断でき、**同じコマンドを再実行するだけで途中から再開**できる。

```bash
# 途中で止まった / 止めた場合、そのまま再実行
./run_build.sh --db 3
```

- 各ファイル処理前に DB に既存データがあるかチェックし、処理済みファイルは瞬時にスキップ
- DB 投入は UPSERT（`ON CONFLICT DO UPDATE`）のため、重複実行しても安全
- 新規ファイルが追加された場合は、再実行すると新規分だけ処理される
- DB を TRUNCATE しない限り、蓄積データは保持される

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
├── sync_rotate.py                 ← ローテーション型差分同期（5分間隔想定）
├── calc_workers.py                ← 最適並列ワーカー数の算出
├── ocr_scan.py                    ← 画像OCR後追いツール
├── export_db.sh                   ← DBダンプ出力
├── import_db.sh                   ← DBダンプ取り込み
├── run_build.sh                   ← 並列ビルド起動 + モニター
├── run_build_single.sh            ← シングルプロセスビルド
├── run_sync_rotate.sh             ← 差分同期実行（launchd から呼ぶ）
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
├── launchd/
│   └── co.gridworld.gridworldrag.sync.plist  ← LaunchAgent テンプレート
├── tests/
│   ├── test_db_escape.py              ← LIKE エスケープ (6)
│   ├── test_sync_rotate_lock.py       ← lockfile PID probe (7)
│   ├── test_drive_fields.py           ← Drive API fields (5)
│   ├── test_disk_check.py             ← 空き容量チェック (4)
│   ├── test_log_rotation.py           ← RotatingFileHandler (3)
│   ├── test_retry_queue.py            ← 再試行キュー (7)
│   ├── test_extract_text_fallback.py  ← 未対応 MIME fallback (12)
│   ├── test_retry_classification.py   ← HTTP 5xx リトライ分類 (16)
│   ├── test_resilience_hardening.py   ← prefix collision + partial (13)
│   ├── test_oauth_refresh_retry.py    ← OAuth refresh retry (4)
│   └── test_build_preflight.py        ← /tmp preflight (5)
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
| `get_changes_start_token(service, drive_id=None)` | Changes API の開始トークンを取得（`drive_id` 指定で特定の共有ドライブ用） |
| `list_changes(service, page_token, drive_id=None)` | 前回以降の変更ファイルを取得（`drive_id` 指定で特定ドライブに限定） |

### `src/db.py`

PostgreSQL + pgvector の操作を担当。

```python
from src.db import connect, insert_chunks, search_similar, lookup_by_url, upsert_file_chunks, file_exists

conn = connect()
insert_chunks(conn, chunks_data)                       # 一括 UPSERT
status = upsert_file_chunks(conn, file_id, chunks)     # 1 ファイル分をアトミックに置換（"added"|"updated"）
exists = file_exists(conn, file_id)                    # チャンク存在チェック
results = search_similar(conn, query_embedding, n_results=5)
doc = lookup_by_url(conn, "https://docs.google.com/spreadsheets/d/.../edit?gid=123")
```

内部ヘルパー `_escape_like_literal(value)` は Drive file ID に含まれる `_` を
PostgreSQL LIKE のメタ文字から守るためのエスケープ関数。`delete_by_file_id` /
`lookup_by_url` / `file_exists` / `upsert_file_chunks` は全てこの helper 経由で
`LIKE ... ESCAPE '\'` を使う。

### `gridworld-rag-mcp/server.py`

Claude Code 用の MCP サーバー。FastMCP ベースで 6 ツールを提供する。

| ツール | 用途 |
|---|---|
| `search` | セマンティック検索（クエリに URL が含まれる場合は `lookup` と併用） |
| `lookup` | Google Drive/Docs の URL からファイル内容を直接取得 |
| `stats` | インデックス DB の統計（総件数・ファイルタイプ・オーナー別集計） |
| `folder_tree` | DB からフォルダ構成ツリーを再構築して表示 |
| `recent_changes` | 直近の差分同期（`sync_rotate.py`）で追加・更新・削除されたファイル一覧 |
| `sync_history` | sync_rotate 実行履歴の一覧・集計（直近 N 日間） |

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
| Claude Code 連携 | MCP サーバー（gridworld-rag-mcp、`search` / `lookup` / `stats` / `folder_tree` / `recent_changes`） |

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

> **注意**: インポート先では `build_parallel.py` や `sync_rotate.py` は不要。MCPサーバーのみ動かせばよい。

## 差分同期の自動化

`sync_rotate.py` は共有ドライブ単位で独立した Changes API トークンを持ち、**5分間隔の頻回実行で変更があったドライブだけ処理する**ローテーション型差分同期。OS ごとに以下の方法で定期実行する:

| OS | 仕組み | 設定ファイル |
|---|---|---|
| **macOS** | launchd LaunchAgent | `launchd/co.gridworld.gridworldrag.sync.plist` |
| **Windows (WSL2)** | Windows Task Scheduler → bat → WSL 内 sh | `scheduler/windows/*.bat` |
| **Linux** | systemd timer（自前設定） | ユーザー定義 |

> **同時運用禁止**: Mac と Windows を同じ Google OAuth クライアント + 同じ共有ドライブに対して両方動かすと、Drive Changes API のトークンが両者で交互に進み変更を取り逃す可能性あり。**どちらか一方のみ** で運用すること。

### Mac 版（launchd）の詳細

### なぜローテーション型か

- 1ドライブ 1 Changes API コールで「変更の有無」を判定できる（変更ゼロなら即スキップ）
- 変更ゼロの状態で 22 ドライブを一巡しても 7 秒程度
- 埋め込みモデル（SentenceTransformer）は変更が1件でも見つかった時にだけロード（遅延ロード）
- `/tmp/gridworldrag_rotate.lock` で多重起動防止

### 耐障害性

- **ログローテーション**: `~/Library/Logs/gridworldrag/sync_rotate.log` に書き込み、Python 標準の `RotatingFileHandler` で 5MB × 3 世代自動管理（最大 15MB）
- **空き容量プリフライトチェック**: PostgreSQL データディレクトリの空き容量が `MIN_FREE_BYTES` (= 1GB) を切ると処理を中止しトークンを進めない
- **DB ディスク満杯検出**: `psycopg2.errors.DiskFull` または "no space" 等のメッセージを検出すると `DiskFullHalt` 例外で処理を中断し、`last_sync_result.disk_full=true` を記録
- **再試行キュー**: 失敗したファイルは `sync_state.failed_files` に積まれ、次回実行時に最優先で再処理。トークン前進が安全になり永久データロスを防ぐ
- **アトミック upsert**: `src/db.py` の `upsert_file_chunks` が 1 ファイル分の delete + insert を単一トランザクションで実行し、途中失敗時も整合性を保つ

### 使い方（手動）

```bash
# 初回のみ: 全ドライブの変更追跡トークンを初期化
./run_sync_rotate.sh --db 3 --init

# 通常実行（全ドライブを巡回）
./run_sync_rotate.sh --db 3

# 特定ドライブのみ
./run_sync_rotate.sh --db 3 --drive 0ABCxyz...
```

実行結果は `sync_state.last_sync_result` に記録され、MCP の `recent_changes` ツールから確認できる。

### launchd (LaunchAgent) への登録

LaunchAgent 定義ファイル: `launchd/co.gridworld.gridworldrag.sync.plist`（リポジトリに含まれるテンプレート）

インストール:
```bash
# LaunchAgents ディレクトリにコピー
cp launchd/co.gridworld.gridworldrag.sync.plist ~/Library/LaunchAgents/

# ロード（これで自動起動＋5分間隔の定期実行が始まる）
launchctl load ~/Library/LaunchAgents/co.gridworld.gridworldrag.sync.plist
```

アンロード:
```bash
launchctl unload ~/Library/LaunchAgents/co.gridworld.gridworldrag.sync.plist
```

手動トリガー（次の5分を待たず即実行）:
```bash
launchctl start co.gridworld.gridworldrag.sync
```

ログ確認:
```bash
# プライマリ: RotatingFileHandler のログ (5MB × 3世代)
tail -f ~/Library/Logs/gridworldrag/sync_rotate.log

# launchd の標準出力・エラー (起動直後の bootstrap エラーはこちら)
tail -f /tmp/gridworldrag_sync.log
tail -f /tmp/gridworldrag_sync.err
```

### 動作仕様

| 項目 | 値 |
|---|---|
| 実行間隔 | 300秒（5分） |
| スリープ中 | 発火せず（復帰後に `RunAtLoad` で1回走る） |
| CPU優先度 | Nice=5（低優先、他作業の邪魔をしない） |
| 前回終了後の最短間隔 | 10秒（ThrottleInterval） |
| 多重起動 | lockfile + PID liveness probe で防止（PID 死亡なら即 takeover、PID 不明時は 20 分 stale） |
| 接続先DB | `--db 3`（plist内で固定） |
| 絶対パス | plist 内にハードコード（マシン固有） |

### トラブルシュート

- **「前回実行中 skip」と毎回出る**: lockfile に書かれた PID が生存中の可能性。プロセスが死んでいれば自動 takeover されるが、即時復旧したいなら手動で `rm /tmp/gridworldrag_rotate.lock`
- **変更があるはずなのに反応しない**: Changes API はトークン発行時点以降の変更しか拾わない。過去の未反映ファイルは拾えない → フル再ビルドが必要
- **PostgreSQL エラー**: `plist` の `EnvironmentVariables.PATH` に ARM 版 `/opt/homebrew/opt/postgresql@17/bin` が入っているか確認
- **`exit 2` で毎回落ちる**: `shutil.disk_usage` がしきい値 (1GB) 未満を検出している可能性。`df -h /opt/homebrew/var/postgresql@17` で確認
- **`retry_pending` が減らない**: 永続的にエラーを出すファイルが失敗キューに溜まっている。`recent_changes` MCP ツールで確認し、必要なら `psql -d gridworldrag_3 -c "DELETE FROM sync_state WHERE key='failed_files'"` でリセット

### Windows 版（Task Scheduler + WSL2）の詳細

v0.2.1 から Windows Task Scheduler 連携を同梱。WSL2 (Ubuntu 22.04+) 内で `run_sync_rotate.sh` を呼び出す薄い bat shim を Task Scheduler に登録する。

```cmd
REM 登録（管理者権限不要）
cd scheduler\windows
register_sync_rotate_task.bat

REM 削除
unregister_sync_rotate_task.bat
```

カスタマイズは `scheduler\windows\run_sync_rotate.bat` の先頭 3 変数（`WSL_DISTRO` / `WSL_USER` / `DB_NUM`）を編集。

**Windows 側動作仕様（デフォルト）:**

| 項目 | 値 |
|---|---|
| 間隔 | 1 分（テスト用、本番は 5 分推奨） |
| Hidden | コンソール非表示 |
| MultipleInstances | IgnoreNew（前回動作中なら新規起動をスキップ） |
| ExecutionTimeLimit | 10 分 |
| RunLevel | Limited（昇格なし） |
| AllowStartIfOnBatteries | 有効 |

**多重起動ガード（二段階）:**
1. Windows 側: `MultipleInstances=IgnoreNew`
2. WSL 側: `/tmp/gridworldrag_rotate.lock`（PID liveness probe, 20 分 stale fallback）

詳細・運用ノートは [scheduler/windows/README.md](./scheduler/windows/README.md) 参照。

## テスト

純ロジック系の単体テストを `tests/` 配下に配置（DB や Drive API のモック不要）。

```bash
# 全テスト実行
for t in tests/test_*.py; do GRIDWORLDRAG_SKIP_CONFIG=1 .venv/bin/python "$t"; done
```

テスト総数: 82 件 (11 ファイル)。外部依存なしで ~2 秒で全件通る。

## 内部運用: secrets の管理（GridWorldOrganization メンバー向け）

`config.env` と `shared_drives_whitelist.txt` は `.gitignore` で公開リポから除外しているため、ローカルで失うと復旧不能になる。そこで、これらだけを保管する**プライベート姉妹リポ**を使って運用する。

- **リポ**: [GridWorldOrganization/GridWorldRAG-secrets](https://github.com/GridWorldOrganization/GridWorldRAG-secrets)（**private**）
- **配置**: このリポと**兄弟ディレクトリ**に clone（`GridWorldRAG/` の中には入れない）
- **接続**: `GridWorldRAG/config.env` と `GridWorldRAG/shared_drives_whitelist.txt` は symlink として secrets リポの実体を指す

```
~/dev/claude_code/dev/
├── GridWorldRAG/                          ← 公開リポ（このリポ）
│   ├── config.env → ../GridWorldRAG-secrets/config.env
│   └── shared_drives_whitelist.txt → ../GridWorldRAG-secrets/shared_drives_whitelist.txt
└── GridWorldRAG-secrets/                  ← private リポ（実体）
    ├── config.env
    └── shared_drives_whitelist.txt
```

### 新環境での復旧手順

```bash
cd ~/dev/claude_code/dev/
git clone https://github.com/GridWorldOrganization/GridWorldRAG.git
git clone https://github.com/GridWorldOrganization/GridWorldRAG-secrets.git
cd GridWorldRAG
ln -s ../GridWorldRAG-secrets/config.env config.env
ln -s ../GridWorldRAG-secrets/shared_drives_whitelist.txt shared_drives_whitelist.txt
```

### 更新時の運用

`GridWorldRAG/config.env` を編集すると symlink 経由で実体（secrets リポ側）が更新されるので、secrets リポで commit/push するだけ：

```bash
cd ~/dev/claude_code/dev/GridWorldRAG-secrets
git add -A && git commit -m "update: config.env" && git push
```

### 備考

- `credentials.json` と `token.pickle` は secrets リポに入れない（GCP で再発行・再認証で復元可能なため）
- OSS 利用者は secrets リポを使わず、`config.env.example` をコピーして自前で設定すれば良い

## ドキュメント

| ドキュメント | 内容 |
|---|---|
| [QUICKSTART.md](./QUICKSTART.md) | 5 ステップでのセットアップ |
| [docs/architecture.md](./docs/architecture.md) | システム全体像・Mermaid 図 |
| [docs/technical.md](./docs/technical.md) | 詳細な処理フロー・ログ仕様・DB スキーマ |
| [docs/vectordb.md](./docs/vectordb.md) | VectorDB 設計の背景 |
| [docs/faq.md](./docs/faq.md) | よくある質問 |
| [docs/benchmarks.md](./docs/benchmarks.md) | 性能実測値 |
| [docs/troubleshooting.md](./docs/troubleshooting.md) | トラブルシュート集約 |
| [docs/roadmap.md](./docs/roadmap.md) | 今後の予定 |
| [docs/release.md](./docs/release.md) | リリース手順 |
| [docs/adr/](./docs/adr/) | 技術選定の意思決定記録 |
| [docs/mac-resident-daemon.md](./docs/mac-resident-daemon.md) | v0.3.0〜v1.0.0 アーキテクチャ計画 |
| [CHANGELOG.md](./CHANGELOG.md) | 変更履歴 |
| [CONTRIBUTING.md](./CONTRIBUTING.md) | コントリビューションガイド |
| [SECURITY.md](./SECURITY.md) | 脆弱性報告の手順 |
| [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md) | 行動規範 |

## コントリビューション

PR 歓迎。詳細は [CONTRIBUTING.md](./CONTRIBUTING.md) 参照。

## セキュリティ

脆弱性を発見した場合は公開 Issue ではなく [GitHub Security Advisory](https://github.com/GridWorldOrganization/GridWorldRAG/security/advisories/new) でプライベートに報告してください。詳細は [SECURITY.md](./SECURITY.md) 参照。

## ロードマップ

v0.3.0 以降は Mac 常駐デーモン + GUI + MCP 型への移行を計画しています。詳細は [docs/roadmap.md](./docs/roadmap.md) および [docs/mac-resident-daemon.md](./docs/mac-resident-daemon.md) 参照。

## 謝辞

本プロジェクトは以下のオープンソースに依拠しています:

- [pgvector](https://github.com/pgvector/pgvector) — PostgreSQL 向けベクトル拡張
- [sentence-transformers](https://github.com/UKPLab/sentence-transformers) — 埋め込みモデル
- [FastMCP](https://github.com/jlowin/fastmcp) — MCP サーバーフレームワーク
- [google-api-python-client](https://github.com/googleapis/google-api-python-client) — Google Drive API
- [langchain-text-splitters](https://github.com/langchain-ai/langchain) — テキスト分割

また、設計検討時に参考にしたプロジェクト:

- [donat-konan33/google-drive-agentic-rag](https://github.com/donat-konan33/google-drive-agentic-rag) — OAuth2 認証と GoogleDriveLoader の実装
- [taylorwilsdon/google_workspace_mcp](https://github.com/taylorwilsdon/google_workspace_mcp) — Google Workspace MCP
- [getomnico/omni](https://github.com/getomnico/omni) — Drive + pgvector + 権限継承の Rust 実装

## ライセンス

[MIT License](LICENSE) © 2025 GridWorld Organization
