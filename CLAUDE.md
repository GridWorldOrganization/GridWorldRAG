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
├── build_parallel.py      # メイン：並列インデックスビルド（タスクキュー方式）
├── build_single.py        # シングルプロセス版（デバッグ用）
├── calc_workers.py        # ワーカー数自動計算
├── ocr_scan.py            # OCR スキャンツール
├── src/
│   ├── config.py          # config.env 読み込み＋設定値管理
│   ├── db.py              # PostgreSQL + pgvector 操作（UPSERT、検索）
│   ├── drive_client.py    # Google Drive API クライアント（認証、テキスト抽出）
│   ├── indexer.py         # チャンク作成、パーミッション抽出等のヘルパー
│   └── monitor_render.py  # モニター画面のレンダリング（Python）
├── gridworld-rag-mcp/
│   ├── server.py          # MCP サーバー（search, lookup, stats）
│   └── run_mcp.sh         # MCP 起動スクリプト
├── run_build.sh           # 2フェーズ実行ランチャー
├── run_build_single.sh    # シングルプロセス版ランチャー
├── run_calc_workers.sh    # ワーカー数計算ランチャー
├── monitor.sh             # リアルタイムモニター（tput sc/rc）
├── setup.sh               # 環境セットアップ
├── schema.sql             # DBスキーマ（ALTER TABLE含む）
├── config.env             # 設定ファイル（Git管理外）
├── config.env.example     # 設定ファイルテンプレート
├── shared_drives_whitelist.txt  # 対象共有ドライブID一覧
├── docs/
│   ├── technical.md       # 技術リファレンス
│   └── vectordb.md        # ベクトルDB関連メモ
├── QUICKSTART.md          # クイックスタートガイド
└── README.md              # プロジェクト概要
```

## アーキテクチャの要点

### 並列インデックスビルド（build_parallel.py）

- **2フェーズ実行**: `--fetch-only`（フォアグラウンド、ファイル一覧取得）→ `--work-only`（バックグラウンド、インデックス構築）
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
- カラム: title, content, embedding, owner, source_url, file_type, modified_at, file_id, sheet_gid, sheet_name, permissions(JSONB)
- **全関数でカーソルを `try/finally` で保護**（例外時もリーク防止）
- **書き込み系は例外時に `rollback()`**（未コミットトランザクション防止）
- `psycopg2`（ソースビルド版）を使用。`psycopg2-binary`（バンドル版）は libssl 重複でクラッシュするため使わない

### モニター（monitor.sh + monitor_render.py）

- `tput sc/rc`（カーソル保存/復元）で画面更新（他の方式は全て失敗した）
- Python でレンダリング、shell でループ制御
- ワーカーステータス: done, loading, ready, rate_limited, rate_limited_running, running

### MCP サーバー（gridworld-rag-mcp/server.py）

- FastMCP ベース、3ツール: `search`（セマンティック検索）, `lookup`（URL直接取得）, `stats`（統計）
- `search` はクエリ内の URL を自動検出し、URL lookup + セマンティック検索を併用
- 埋め込みモデルは遅延ロード
- プロジェクトスコープで登録済み: `claude mcp add gridworld-rag-mcp --scope project`

## 開発で踏んだ落とし穴

- **EXPORT_MIME_MAP にスプレッドシートを含めると最初のシートしかCSV exportされない** → Sheets API で個別取得に変更
- **multiprocessing.Queue に大きなデータを入れるとパイプがブロックする** → Pickle ファイルに分離
- **モニターのカーソル制御**: `\033[nA]`, `clear`, `\033[H]` は全て問題あり → `tput sc/rc` が正解
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

## GitHub Issues（既知の課題）

- #1: DB サイズ見積もり
- #2: ファイル単位の進捗表示
- #3: Enter キー表示バグ
