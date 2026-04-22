# WinServerRAG

[![GridJapan](https://img.shields.io/badge/built_by-GridJapan-0a84ff)](#%E9%96%8B%E7%99%BA%E6%A1%88%E4%BB%B6%E3%81%AE%E3%81%94%E7%9B%B8%E8%AB%87)
[![Windows Native](https://img.shields.io/badge/runtime-Windows_11-0078d4)](.)
[![PostgreSQL](https://img.shields.io/badge/db-PostgreSQL_17_%2B_pgvector-336791)](.)
[![GPU](https://img.shields.io/badge/embedding-RTX_4070_CUDA_12.4-76b900)](.)

Windows 常駐の RAG（Retrieval-Augmented Generation）サーバー。
Google Drive の共有フォルダをセマンティック検索できるようにし、
Claude Cowork / Claude Desktop / 任意の MCP クライアントから **MCP (Model Context Protocol) 経由で遠隔検索** できます。

> ⚡ **実測性能**: warm search **840ms** (AWS API Gateway 経由、リランカー込み、RTX 4070 SUPER)
> 🔒 **セキュリティ**: Basic Auth + per-drive 分離スキーマ + PBKDF2-SHA256 + AWS IAM 最小権限
> 🔧 **運用**: 4 並列ワーカー、自動差分同期、ゾンビ GC、障害耐性

---

## このリポジトリについて

これは **GridJapan** が実際に自社で運用している業務ツールのソースコードです。
社内 Google Drive の資料（数万件）を横断的に検索し、
Claude Cowork から遠隔で呼び出すために構築しました。

> 📖 **1 台の PC のバッチから GPU 駆動の AWS サーバーレスに変わるまでの進化記録**: [docs/EVOLUTION.md](./docs/EVOLUTION.md)

### 開発案件のご相談

このプロダクトのようなシステムを「自社にも作りたい」と感じた方へ。

GridJapan は **AI × 業務システム** の開発を請け負っています。
以下のようなテーマでご相談歓迎です：

- 社内ナレッジを **LLM / RAG** 経由で検索できる仕組み
- **Claude / OpenAI / 独自 LLM** の業務組み込み（MCP / Function Calling / Agent）
- **Windows サーバー** 上で動く AI システム（GPU 活用、常駐サービス化）
- **Google Workspace / Microsoft 365** など既存ツールとのネイティブ連携
- 既存の業務データから RAG データセットを構築するパイプライン

**お問い合わせ**: [tobisako@gridworld.co](mailto:tobisako@gridworld.co)
または GitHub Issue でお気軽にどうぞ。

---

## What is this?

WinServerRAG is a Windows-native RAG server we built and run internally at GridJapan.
It indexes Google Drive shared folders into pgvector, serves a Web monitor
and an Electron mini-monitor, and exposes search through the Model Context
Protocol so Claude Cowork on another machine can query it remotely through
an AWS serverless bridge.

The code in this branch is the real production copy we use day-to-day.

We are a small engineering shop based in Japan. If your team wants something
like this built for your business, reach out: **tobisako@gridworld.co**.

---

## できること

- Google Drive の共有フォルダ群を自動的に索引（構造認識チャンク + ベクトル）
- **ビルドは自動**: 有効化されたドライブを 4 ワーカー並列で常時追跡（変更検知は Changes API）
- **マルチスレッド** (デフォルト 4、`DAEMON_WORKER_THREADS` で調整)
- **Reranker**: BGE-m3 cross-encoder で Top-K 並び替え (`ENABLE_RERANKER=1`)
- **2 画面**: 管理者用「ビルド画面」 + 「MCP 検索設定画面」
- **Electron ミニモニター** (always-on-top、500ms 更新)
- **MCP サーバー** (Streamable HTTP + Basic Auth、クエリログ付き) → [MCP 接続ガイドはこちら](./MCP.md)
- **Eval Suite** (`tests/eval/`) — 品質の定量モニター
- **自動バックアップ** (`scripts/backup.bat`) — 日次 7 + 週次 4 世代

## アーキテクチャ

```
[Google Drive 共有フォルダ] → rag_daemon (N threads) → PostgreSQL + pgvector (per-FD schema)
                                      │
                                      ▼
                             control_api (FastAPI)
                              /            \
                             /              \
                   Web monitor              MCP server  ←── Claude Cowork (remote)
                   (localhost)              /mcp        (Basic Auth + Streamable HTTP)
                                            │
                                         Electron mini-monitor (desktop)
```

## クイックスタート

1. Python 3.12+ と PostgreSQL 17 + pgvector を用意 (Windows ネイティブ)
2. `python -m venv .venv && .venv\Scripts\activate`
3. `pip install -r requirements.txt`
4. `config\config.v2.env.example` を `config\config.v2.env` にコピーして編集
5. DB 初期化: `python -m src.db_init`
6. API 起動: `scripts\run_api.bat`
7. デーモン起動 (別ターミナル): `scripts\run_daemon.bat`
8. ブラウザで `http://127.0.0.1:17600/` → 管理画面
9. Electron ミニモニター: `scripts\run_mini.bat`
10. MCP 公開: [MCP.md](./MCP.md) 参照
11. バックアップは手動で `scripts\backup.bat` を実行（自動化は `scripts\install_service.md` 参照、Task Scheduler は使用禁止）

## 廃案（設計書にあるが採用しない）

- **リモートモニター別アプリ** (旧 Phase 3 案): Web モニターをトンネルで公開 + Electron ミニモニターで十分。別プロセスの Tauri を作る価値無し
- **スレッド数の動的変更 UI**: 1 ユーザー運用では固定で十分。`DAEMON_WORKER_THREADS` で初期値のみ
- **per-user MCP 検索スコープ**: 複雑度に見合う価値無し。`fd_registry.search_enabled` でグローバル管理

## 画面

### ビルド画面 (`/` → ビルドタブ)

- ワーカー N スレッドの現在状態（どのドライブ・どのファイル・進捗バー）
- スレッド数スケーラ [−] [4] [+]、MIN 1 / MAX 10 でライブ変更
- 共有フォルダ一覧: 行クリックで **ビルド ON/OFF** トグル
- 各行に [再構築（初期化）] [削除] ボタン（destructive はダイアログ確認）

### MCP 検索設定画面 (`/` → MCP タブ)

- **MCP ログインユーザー** 管理
  - 初回 seed は `WINSERVERRAG_SEED_USERS="user1:pw1,user2:pw2"` の環境変数経由 (バンドルデフォルト無し)
  - UI から 追加・パスワード変更・削除 が可能
- **MCP 検索スコープ**: どの共有ドライブを遠隔検索の対象にするか行クリックで ON/OFF

### Electron ミニモニター

- 小窓、always-on-top、500ms 更新
- 稼働中はスピナー回転、ビルド進捗シマー、心拍ブリンク
- デーモン死亡 (2 分無心拍) で赤表示
- [Monitor を開く] ボタンで Web モニターをブラウザで開く

## スキーマ (PostgreSQL)

- `public.fd_registry` : ドライブ登録・ビルド ON/OFF・検索スコープ ON/OFF
- `public.mcp_users`   : MCP ログインユーザー (PBKDF2-SHA256)
- `public.daemon_workers` : スレッドのライブ状態 (心拍)
- `public.daemon_config` : 動的設定 (worker_count 等)
- `fd_<drive_id>.documents` : ドライブごとの chunks + VECTOR(768)

## MCP (遠隔 RAG 検索)

**別 PC から Claude Cowork で検索したい場合** → [MCP.md](./MCP.md) に詳細手順があります。

提供される MCP ツール:

| tool | 引数 | 説明 |
|---|---|---|
| `list_drives` | — | 検索スコープのドライブ一覧 |
| `search` | `query, n_results=10, owner?` | セマンティック検索 |
| `lookup` | `url` | 特定 Drive URL の全文取得 |
| `stats` | — | インデックス統計 |

## 設定ファイル

優先順位: `config/config.v2.env` → `config/config.env` (legacy fallback)

| キー | 既定 | 説明 |
|---|---|---|
| `DAEMON_WORKER_THREADS` | `4` | 初期スレッド数 (UI で 1..10 変更可) |
| `DAEMON_ROTATE_INTERVAL_SEC` | `300` | ビルド sweep 間隔 |
| `API_HOST` / `API_PORT` | `127.0.0.1` / `17600` | 管理 API |
| `API_BEARER_TOKEN` | 空 | LAN 公開時は必須 (空だと localhost のみ) |
| `EMBEDDING_MODEL` | `paraphrase-multilingual-mpnet-base-v2` | 768 次元 |
| `GOOGLE_OAUTH_CLIENT_ID/SECRET` | — | Google OAuth 認証情報 |

## 運用 (Windows サービス化)

NSSM で常駐サービス化: [scripts/install_service.md](./scripts/install_service.md)

## 耐障害性

- `lockfile` で多重起動防止 (PID 生存確認、stale 検知)
- `pg_try_advisory_lock` でプロセス跨ぎの FD 排他制御
- デーモン `zombie_cleanup` で stale worker 行を 30 秒ごとに GC
- UI は worker heartbeat 120 秒超で stale 判定しアニメ停止
- stats エンドポイントは in-memory キャッシュで重書き込み中も即レス

## ライセンス

MIT（本リポジトリ上の内容）。Google API・pgvector 等の依存は各自のライセンスに従う。
