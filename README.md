# WinServerRAG

Windows 常駐の RAG サーバー。Google Drive 共有フォルダ単位で DB を持ち、
Claude Cowork / Claude Desktop から **MCP (Model Context Protocol) 経由で遠隔検索可能**。
既存 GridWorldRAG のロジックを Windows ネイティブ向けに移植したもの。

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
11. バックアップ登録: `scripts\install_service.md` の Task Scheduler セクション

## 廃案（設計書にあるが採用しない）

- **Mac リモートモニター** (旧 Phase 3): Web モニターをトンネルで公開すれば十分。Tauri 別アプリを作る価値無し
- **スレッド数の動的変更 UI**: 1 ユーザー運用では固定で十分。`DAEMON_WORKER_THREADS` で初期値のみ
- **per-user MCP 検索スコープ**: 複雑度に見合う価値無し。`fd_registry.search_enabled` でグローバル管理

## 画面

### ビルド画面 (`/` → ビルドタブ)

- ワーカー N スレッドの現在状態（どのドライブ・どのファイル・進捗バー）
- スレッド数スケーラ [−] [4] [+]、MIN 1 / MAX 10 でライブ変更
- 共有フォルダ一覧: 行クリックで **ビルド ON/OFF** トグル
- 各行に [再構築（初期化）] [削除] ボタン（destructive はダイアログ確認）

### MCP 検索設定画面 (`/` → MCP タブ)

- **MCP ログインユーザー** 管理 (初期値: `tobisako` / `izumi`、PW は `admin`)
  - 追加・パスワード変更・削除
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
