# Troubleshooting

README・QUICKSTART に散在しているトラブルシュート情報の集約。解決しない場合は [FAQ](./faq.md) → [GitHub Discussions](https://github.com/GridWorldOrganization/GridWorldRAG/discussions) → [Issue](https://github.com/GridWorldOrganization/GridWorldRAG/issues) の順で確認してください。

## セットアップ系

### `PostgreSQL に接続できません` / `connection refused`

PostgreSQL サービスが起動していません。

```bash
# Mac
brew services start postgresql@17

# Ubuntu/WSL
sudo systemctl start postgresql
```

### `config.env が見つかりません`

初回セットアップで `config.env.example` をコピーしていない場合:

```bash
cp config.env.example config.env
# 必要な値（OAuth Client ID/Secret 等）を設定
vi config.env
```

### `torch` インストール失敗

Intel 版 Python を使っている可能性。Apple Silicon Mac では ARM 版 Python 必須:

```bash
# ARM 版 Python を確認
which python3.12
# /opt/homebrew/... であれば正解
# /usr/local/... なら Intel 版 → .venv を削除して再作成
rm -rf .venv
/opt/homebrew/opt/python@3.12/bin/python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### `pgvector extension not found` / `extension "vector" does not exist`

pgvector がインストールされていない / PostgreSQL から見つかっていない:

```bash
# Mac
brew install pgvector

# 既存 DB で拡張を有効化
psql -d gridworldrag_1 -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

### `psycopg2` インストール失敗（Linux/WSL）

開発ヘッダが不足:

```bash
sudo apt-get install -y libpq-dev libssl-dev build-essential
pip install --no-binary :all: psycopg2==2.9.11
```

## 認証系

### Google 認証エラー / `invalid_grant` / `token has been expired`

`token.pickle` を削除して再実行:

```bash
rm token.pickle
./run_build.sh  # ブラウザが開き、再認証を求められる
```

### OAuth リダイレクト URI エラー

Google Cloud Console で OAuth クライアントの設定を確認:

- アプリケーションの種類: **デスクトップ アプリ**（Web アプリではない）
- リダイレクト URI を明示設定している場合は削除（デスクトップアプリは `http://localhost` 自動）

### `insufficient permissions` / `scope` エラー

現プロジェクトは `drive.readonly` スコープのみ要求。既存トークンが別スコープで発行されている場合:

```bash
rm token.pickle
# config.env で OAuth Client ID/Secret を確認してから再実行
./run_build.sh
```

## ビルド系

### ビルドが途中で止まり Ctrl+C も効かない

以下の順で対応:

1. **1 回 Ctrl+C**: graceful shutdown（10 秒待ってワーカー kill）
2. **2 回目 Ctrl+C**: 即座に force kill
3. それでも止まらない場合、別ターミナルで:

```bash
pkill -f build_parallel.py
pkill -f monitor.sh
```

4. プロセス残存確認:

```bash
ps aux | grep gridworldrag
```

### `/tmp` 空き容量不足で exit 2

`BUILD_MIN_TMP_FREE_BYTES` デフォルト 500 MB に対して不足:

```bash
df -h /tmp
# 500 MB 以上空ける、または config.env で閾値を下げる
# BUILD_MIN_TMP_FREE_BYTES=100000000  # 100 MB に緩和
```

### Sheets API 429 が多発する

`multiprocessing.Semaphore(2)` で制御していても、大量のスプレッドシートがあると発生:

```bash
# config.env
WORKER_START_INTERVAL_SEC=15   # 10 → 15 に増やす
PARALLEL_WORKERS=6             # 8 → 6 に減らす
```

### `embedding column is not the right size` エラー

埋め込みモデルの次元数と DB スキーマ (`VECTOR(768)`) が一致していない:

```bash
# 現在のモデル確認
grep EMBEDDING_MODEL src/config.py

# 768 次元のモデル（paraphrase-multilingual-mpnet-base-v2 等）を使う
# または schema.sql を VECTOR(N) に変更して DB 再作成
```

### 整合性チェックで `警告 DB X/Y (N 件不足)`

一部ファイルの処理が失敗している。`/tmp/gridworldrag_build.log` を確認し、エラー内容に応じて対応:

- ネットワーク系: 再実行で復旧
- PDF / OCR 系: ファイル個別に `ocr_scan.py` でリトライ
- 不明なエラー: Issue で報告

## 同期系（sync_rotate.py）

### 「前回実行中 skip」と毎回出る

lockfile の PID が生存しているか死んでいるか:

```bash
cat /tmp/gridworldrag_rotate.lock
# 書かれた PID が生存中なら正常（本当に他プロセスが動いている）
ps -p <PID>

# プロセスが死んでいれば自動 takeover されるはずだが、
# 即時復旧したいなら手動削除
rm /tmp/gridworldrag_rotate.lock
```

### 変更があるはずなのに反応しない

Changes API はトークン発行時点以降の変更しか拾わない。過去の未反映ファイルを取り込むには:

```bash
# オプション 1: 再ビルド（推奨）
./run_build.sh

# オプション 2: トークンを再初期化（全ドライブの現時点を新基準に）
./run_sync_rotate.sh --db 1 --init
```

### `retry_pending` が減らない

永続的エラーのファイルが失敗キューに溜まっている:

```bash
# MCP の recent_changes で確認
# Claude Code で: /mcp gridworld-rag-mcp recent_changes

# 必要ならリセット
psql -d gridworldrag_1 -c "DELETE FROM sync_state WHERE key='failed_files'"
```

### `disk_full_preflight` で毎回 abort

```bash
df -h /opt/homebrew/var/postgresql@17
# 1GB 以上空ける
```

### launchd でエラーが出ている

```bash
# LaunchAgent のログ確認
tail -50 /tmp/gridworldrag_sync.err

# plist 内の PATH に ARM 版が含まれているか確認
plutil -p ~/Library/LaunchAgents/co.gridworld.gridworldrag.sync.plist | grep PATH
# /opt/homebrew/opt/postgresql@17/bin が含まれているべき
```

### Windows Task Scheduler でタスクが実行されない

```cmd
REM タスクの状態確認
schtasks /query /tn "gridworldrag_sync_rotate" /fo LIST /v

REM 手動実行してエラーを確認
schtasks /run /tn "gridworldrag_sync_rotate"

REM WSL 内からも実行可能か確認
wsl -d Ubuntu -u tobi -- bash -c "cd /mnt/c/claude_code/dev/GridWorldRAG && ./run_sync_rotate.sh"
```

## MCP 系

### Claude Code から MCP が見つからない

```bash
# 登録状態確認
claude mcp list

# 再登録
claude mcp remove gridworld-rag-mcp
claude mcp add gridworld-rag-mcp -- python gridworld-rag-mcp/server.py --db 1

# Claude Code 再起動で反映
```

### MCP の search が遅い（初回）

SentenceTransformer の遅延ロードで初回は 5〜10 秒かかります（設計上）。v0.7.0 で daemon IPC 経由に切替予定（[roadmap.md](./roadmap.md)）。

### `connection closed` / `MCP handshake timeout`

MCP サーバー起動中にエラーが発生している可能性:

```bash
# 直接起動してエラー確認
python gridworld-rag-mcp/server.py --db 1

# config.env が読めるか
python -c "from src.config import DB_NAME; print(DB_NAME)"
```

## DB 系

### DB に接続できるが結果が 0 件

```bash
psql -d gridworldrag_1 -c "SELECT COUNT(*) FROM documents;"
# 0 件ならビルドが未完了 / 失敗

# ビルド実行
./run_build.sh
```

### DB を完全リセットしたい

```bash
# オプション 1: TRUNCATE（高速、スキーマ残す）
psql -d gridworldrag_1 -c "TRUNCATE documents; TRUNCATE sync_state;"

# オプション 2: DB 再作成（完全リセット）
dropdb gridworldrag_1
createdb gridworldrag_1
psql -d gridworldrag_1 -f schema.sql
```

### 複数 DB（`gridworldrag_0`, `gridworldrag_1` 等）を使い分けたい

`--db N` オプションを付けて全コマンドを呼ぶ:

```bash
./run_build.sh --db 2
./run_sync_rotate.sh --db 2
claude mcp add gridworld-rag-mcp-v2 -- python gridworld-rag-mcp/server.py --db 2
```

## クラッシュ系

### Python プロセスが SIGSEGV / SIGABRT で落ちる

v0.1.1 以前の `psycopg2-binary` 版で発生していた SSL 重複問題（[ADR 0003](./adr/0003-psycopg2-source-over-binary.md)）の可能性。現行版でこの症状が出る場合:

```bash
# 1. psycopg2 が source build 版か確認
pip show psycopg2 psycopg2-binary
# psycopg2 のみインストールされているべき

# 2. 両方入っていたらアンインストール
pip uninstall -y psycopg2 psycopg2-binary
pip install --no-binary :all: psycopg2==2.9.11
```

### Out of Memory

- `PARALLEL_WORKERS` を下げる（1 ワーカー約 420 MB + PG 接続）
- `BATCH_SIZE` を下げる（デフォルト 100）
- Docker 利用時は `--memory` 制限を確認

## それでも解決しない場合

1. [FAQ](./faq.md) を再確認
2. [GitHub Discussions](https://github.com/GridWorldOrganization/GridWorldRAG/discussions) で類似事例を検索
3. [Issue](https://github.com/GridWorldOrganization/GridWorldRAG/issues) を検索
4. 新規 Issue を立てる（bug_report.md テンプレート使用）

Issue 立てる時に含めてほしい情報:

- OS / Python / PostgreSQL / GridWorldRAG のバージョン
- 再現手順
- 期待した挙動 vs 実際の挙動
- 関連するログ（機密値を除く）
- `config.env` の関連部分（機密値除く）
