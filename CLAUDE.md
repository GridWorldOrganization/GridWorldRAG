# GridWorldRAG

## プロジェクト概要

Google Drive（共有ドライブ、ホワイトリスト指定）のドキュメントを PostgreSQL + pgvector にインデックスし、セマンティック検索を提供する RAG システム。

## M2 Mac 環境（重要）

このマシンは **Apple M2 Max（ARM64）** だが、Intel 用の Homebrew（`/usr/local`）と ARM 用の Homebrew（`/opt/homebrew`）が共存している。

- **必ず ARM 版（`/opt/homebrew`）を使うこと**
- Intel 版（`/usr/local`）の Python / PostgreSQL を使うと torch 等のパッケージが古いバージョンしか入らず動作しない
- venv 作成時は `/opt/homebrew/opt/python@3.12/bin/python3.12` を使う
- PostgreSQL は `/opt/homebrew/opt/postgresql@17/bin/` を使う

```bash
# 正しいコマンド例
/opt/homebrew/opt/python@3.12/bin/python3.12 -m venv .venv
export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
```

### なぜこうなったか

- Anaconda（Intel x86_64 版）が `/usr/local` の Homebrew と共にインストールされていた
- `python3` コマンドが Anaconda の Python 3.8（x86_64）を指していた
- Intel 版 Python で作った venv では PyTorch 2.2.2 が上限（macOS x86_64 の wheel 配布が 2.2.2 で終了）
- sentence-transformers 5.x は PyTorch 2.4+ を要求するため動作しなかった
- ARM 版 Python に切り替えたことで PyTorch 2.11.0 がインストール可能になり解決

## ファイル構成

```
GridWorldRAG/
├── build_parallel.py       # メイン：並列インデックスビルド（タスクキュー方式）
├── build_single.py         # シングルプロセス版（デバッグ用）
├── sync_rotate.py          # ローテーション型差分同期（5分間隔 launchd 実行）
├── calc_workers.py         # ワーカー数自動計算
├── ocr_scan.py             # OCR スキャンツール
├── src/
│   ├── config.py           # config.env 読み込み＋設定値管理
│   ├── db.py               # PostgreSQL + pgvector 操作（UPSERT、検索、upsert_file_chunks）
│   ├── drive_client.py     # Google Drive API クライアント（認証、テキスト抽出、Changes API）
│   ├── indexer.py          # チャンク作成、パーミッション抽出等のヘルパー
│   └── monitor_render.py   # モニター画面のレンダリング（Python）
├── gridworld-rag-mcp/
│   ├── server.py           # MCP サーバー（search, lookup, stats, folder_tree, recent_changes）
│   └── run_mcp.sh          # MCP 起動スクリプト
├── launchd/
│   └── co.gridworld.gridworldrag.sync.plist  # LaunchAgent 定義（5分間隔）
├── tests/                  # 単体テスト（82件、11ファイル）
│   ├── test_db_escape.py           # LIKE エスケープ (6)
│   ├── test_sync_rotate_lock.py    # lockfile PID probe (7)
│   ├── test_drive_fields.py        # Drive API fields (5)
│   ├── test_disk_check.py          # 空き容量チェック (4)
│   ├── test_log_rotation.py        # RotatingFileHandler (3)
│   ├── test_retry_queue.py         # 再試行キュー (7)
│   ├── test_extract_text_fallback.py  # 未対応 MIME フォールバック (12)
│   ├── test_retry_classification.py   # HTTP 5xx リトライ分類 (16)
│   ├── test_resilience_hardening.py   # prefix collision + partial (13)
│   ├── test_oauth_refresh_retry.py    # OAuth token refresh retry (4)
│   └── test_build_preflight.py        # /tmp 空き容量プリフライト (5)
├── run_build.sh            # 2フェーズ実行ランチャー
├── run_build_single.sh     # シングルプロセス版ランチャー
├── run_sync_rotate.sh      # sync_rotate 起動スクリプト
├── run_calc_workers.sh     # ワーカー数計算ランチャー
├── monitor.sh              # リアルタイムモニター（相対カーソルアップ方式）
├── setup.sh                # 環境セットアップ
├── schema.sql              # DBスキーマ（ALTER TABLE含む）
├── config.env              # 設定ファイル（Git管理外、secrets リポの symlink）
├── config.env.example      # 設定ファイルテンプレート
├── shared_drives_whitelist.txt  # 対象共有ドライブID一覧（symlink）
├── docs/
│   ├── technical.md        # 技術リファレンス
│   └── vectordb.md         # ベクトルDB関連メモ
├── QUICKSTART.md           # クイックスタートガイド
├── CHANGELOG.md            # 変更履歴
└── README.md               # プロジェクト概要
```

## アーキテクチャの要点

### 並列インデックスビルド（build_parallel.py）

- **3フェーズ実行**: `--fetch-only`（Phase 1、ファイル一覧取得）→ `--split-only`（Phase 2、タスク分解、即完了）→ `--work-only`（Phase 3、VectorDB 作成）
- **resume プロンプト**: run_build.sh 起動時に filelist.pkl が残っていれば「resume しますか？ [Y/n]」を表示、Y（デフォルト）で Phase 1 スキップ、N でホワイトリスト更新後の再取得
- **/tmp 空き容量プリフライト**: `BUILD_MIN_TMP_FREE_BYTES`（デフォルト 500MB）未満で exit(2)
- **タスクキュー方式**: `multiprocessing.Queue` + sentinel `None` でワーカー終了制御
- **Pickle IPC**: 大きなタスクデータは `/tmp/gridworldrag_taskdata.pkl` に分離（Queue のパイプブロッキング回避）
- **ラウンドロビン順序**: 全ドライブの (1/n) → (2/n) → ... の順でキューに投入（公平分散）
- **タスク分割**: `TASK_SPLIT_THRESHOLD`（デフォルト5000）超のドライブはパート分割
- **ワーカー順次起動**: `WORKER_START_INTERVAL_SEC` 間隔で起動（API 負荷分散）
- **Sheets API セマフォ**: `multiprocessing.Semaphore(2)` で同時アクセス制限（429 レート制限対策）
- **エラーフォールバック**: 処理失敗時は `[エラー] filename` をDBに書き込み（ファイル名は記録される）
- **整合性チェック**: ビルド完了時にDB内のファイルIDを検証
- **ワーカー例外保護**: `_worker` → `_worker_main` 分離で致命的エラーでも `conn.close()` + `results_queue` 応答を保証
- **DB接続復旧**: `insert_chunks` 失敗時に接続を再作成して処理継続

### Google Drive API（drive_client.py）

- `_api_call_with_retry()`: 指数バックオフ + レート制限コールバック
- **httplib2 ソケットタイムアウト**: `authenticate()` / `get_sheets_service()` で `httplib2.Http(timeout=DRIVE_DOWNLOAD_TIMEOUT_SEC)` を設定。daemon スレッドによるタイムアウトは廃止（SSL double-free 防止）
- スプレッドシート: Sheets API でシート別にデータ取得（CSV export ではない）
- 対応ファイルタイプ: Docs, Sheets（シート別）, Slides, PDF, 画像（OCR）, 動画, 音声, フォルダ
- Lambda クロージャの注意: `lambda sr=sheet_range:` でループ変数をキャプチャ

### DB（db.py）

- pgvector 768次元（multi-qa-mpnet-base-dot-v1）
- UPSERT でべき等操作
- `lookup_by_url()`: gid パラメータ対応（スプレッドシートのシート特定）
- NUL 文字（`\x00`）は `insert_chunks()` で除去（PDF テキストに含まれることがある）
- カラム: drive_file_id, title, content, chunk_index, owner, source_url, file_type, drive_modified_at, embedding, sheet_gid, sheet_name, permissions(JSONB), partial_content, folder_path, created_at
- **全関数でカーソルを `try/finally` で保護**（例外時もリーク防止）
- **書き込み系は例外時に `rollback()`**（未コミットトランザクション防止）
- `psycopg2`（ソースビルド版）を使用。`psycopg2-binary`（バンドル版）は libssl 重複でクラッシュするため使わない
- **LIKE エスケープ**: `_escape_like_literal()` で `_` `%` `\` をエスケープ。Drive file ID が `_` を含むため、全 LIKE クエリに `ESCAPE '\'` を必須
- `upsert_file_chunks(conn, file_id, chunks)`: 1 ファイル分の delete + insert を単一トランザクションで実行、`"added"|"updated"` を返す
- `file_exists(conn, file_id)`: チャンク存在チェック（build_parallel.py の resume 判定に使用）
- `insert_chunks` と `delete_by_file_id` に `commit=False` フラグあり（upsert_file_chunks での合成用）

### モニター（monitor.sh + monitor_render.py）

- 相対カーソルアップ方式（`\033[NA]` で前回印刷行数分だけ戻る）で画面更新
- Python でレンダリング、shell でループ制御
- ワーカーステータス: done, loading, ready, rate_limited, rate_limited_running, running

### MCP サーバー（gridworld-rag-mcp/server.py）

- FastMCP ベース、6ツール: `search`（セマンティック検索）, `lookup`（URL直接取得）, `stats`（統計）, `folder_tree`（DB からフォルダツリー再構築）, `recent_changes`（直近の差分同期結果）, `sync_history`（sync_rotate 実行履歴・集計）
- `search` はクエリ内の URL を自動検出し、URL lookup + セマンティック検索を併用
- 埋め込みモデルは遅延ロード（初回 search 時にロード、FastMCP tool 呼び出しは serialize されるためスレッド安全性は問題なし）
- プロジェクトスコープで登録済み: `claude mcp add gridworld-rag-mcp --scope project`

### ローテーション型差分同期（sync_rotate.py）

- 共有ドライブごとに独立した Changes API ページトークンを保持（`sync_state.rotate_token_<drive_id>`）
- 1実行 = 全ドライブを一巡チェック。変更ゼロのドライブは Changes API 1コールで即スキップ
- 22ドライブ×変更ゼロで実測 **約7秒**/実行
- 埋め込みモデルは変更発生時のみ遅延ロード
- `/tmp/gridworldrag_rotate.lock` で多重起動防止（PID liveness probe → mtime fallback 20分 stale）
- launchd の LaunchAgent（`launchd/co.gridworld.gridworldrag.sync.plist`）から5分間隔実行
- sync.py（旧実装）は削除済み。`drive_client.list_changes(service, token, drive_id=None)` に統合

### 耐障害性（sync_rotate.py）

- **ログローテーション**: `~/Library/Logs/gridworldrag/sync_rotate.log` へ `logging.handlers.RotatingFileHandler`（5MB × 3世代、最大15MB）で書き込み。全 `print()` は logger 呼び出しに置換済み
- **空き容量プリフライトチェック**: `shutil.disk_usage()` で PG データディレクトリ `/opt/homebrew/var/postgresql@17` の空き容量を測定、`MIN_FREE_BYTES=1GB` 未満なら abort マーカーを `sync_state` に記録して `exit(2)`（トークンは進めない）
- **DB ディスク満杯検出**: `_is_disk_full_error()` が `psycopg2.errors.DiskFull` および "no space" / "disk full" / "out of space" 等のメッセージ文字列を検出、`DiskFullHalt` 例外で処理ループを halt
- **failed_files 再試行キュー**: `sync_state.failed_files` に JSON で失敗ファイルID + drive_id を積み、次回実行時に `_retry_failed_files()` が `files().get()` で再取得 → upsert。トークン前進が安全になる（永久データロス防止）
- **アトミック upsert**: `upsert_file_chunks()` が delete + insert を単一トランザクションで実行、途中失敗時も整合性を保つ
- **last_sync_result 拡張フィールド**: `retry_recovered`, `retry_pending`, `disk_full`, `aborted`, `reason`（MCP の recent_changes で可視化）

## 開発で踏んだ落とし穴

- **EXPORT_MIME_MAP にスプレッドシートを含めると最初のシートしかCSV exportされない** → Sheets API で個別取得に変更
- **multiprocessing.Queue に大きなデータを入れるとパイプがブロックする** → Pickle ファイルに分離
- **モニターのカーソル制御**: `clear`, `\033[H]` は全て問題あり。`tput sc/rc` はサブプロセスが ESC 7 を発行すると保存位置が上書きされ 20-30% の確率で表示崩れ → 相対カーソルアップ（`\033[NA]` + 前回行数追跡）が正解
- **Ctrl+C でゾンビプロセスが残る** → `p.daemon=True` + SIGTERM ハンドラ + `terminate()` → `join(timeout=10)` → `kill()`
- **Sheets API 429**: 8ワーカーで 60req/min 超過 → `Semaphore(2)` で解決
- **PDF の NUL 文字**: PostgreSQL が `\x00` を拒否 → `insert_chunks()` で除去
- **config.py で `os.environ[]` を使うと CI/テストで KeyError** → `os.environ.get()` に変更
- **ログの print が flush されない** → `flush=True` を全ワーカーに追加
- **daemon スレッドでのタイムアウト制御は SSL クラッシュを引き起こす** → 放棄スレッドが SSL ソケットを掴んだまま残り、2スレッドが同じ SSL バッファを同時アクセス → double-free (SIGABRT) / NULL deref (SIGSEGV)。httplib2 のソケットタイムアウト (`Http(timeout=N)`) に移行して解決
- **psycopg2-binary はバンドル libssl を同梱する** → Python の `_ssl` モジュールと2つの OpenSSL が同一プロセスに共存し SSL 不安定化。ソースビルド版 `psycopg2` に切り替えて libssl を統一

## 設定値の注意

- `TASK_SPLIT_THRESHOLD`: テスト時は 200 に下げていた。本番は 5000
- `WORKER_START_INTERVAL_SEC`: ユーザーが 10 に変更済み（デフォルト 5）
- `GRIDWORLDRAG_SKIP_CONFIG=1`: config.env を読まずに起動（CI/テスト用）

## Secrets の管理

- `config.env` と `shared_drives_whitelist.txt` は private リポ [GridWorldOrganization/GridWorldRAG-secrets](https://github.com/GridWorldOrganization/GridWorldRAG-secrets) に保管
- 実体は `../GridWorldRAG-secrets/` 兄弟ディレクトリ、このリポ内のファイルは symlink
- 更新時は secrets リポ側で `git add/commit/push`
- 復旧手順は README.md の「内部運用: secrets の管理」セクション参照

## AI の応答ルール

- 作業指示を受けて完了した場合、必ず「完了しました」等の完了報告を発言すること。無言で終わらない
- 質疑応答形式（Q&A）で書かない。コンパクトに
- ログファイル（`.log`）は書き込み専用。**内部処理で `.log` を読み込まない**こと
- テンポラリ情報はオンメモリ変数で対処。不要なファイルを生成しない

## workspace-mcp の使い方

- workspace-mcp は開発中の確認・検証用途でのみ使用する
- 完成コード（Python）には MCP を使わない。`google-api-python-client` で直接 Google API を呼ぶ

## GitHub Issues（全て closed）

- #1〜#9: v0.1.2 で対応済み（DB 容量推計、ファイル内進捗、Enter キー問題、PDF タイムアウト、Sheets retry 設定、MPS workaround、Telegram 通知、sync_history、failed_files TTL）
- #10: lockfile PID liveness probe（v0.1.3）
- #11: OAuth token refresh retry（v0.1.3）
- #12: build_parallel /tmp 空き容量プリフライト（v0.1.3）
