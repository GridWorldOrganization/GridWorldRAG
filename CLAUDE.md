# GridWorldRAG (Windows canonical)

## GitHub リポジトリ

- **URL**: https://github.com/GridWorldOrganization/GridWorldRAG
- **公開リポジトリ**（public）— Windows 11 ネイティブの単一系統
- Mac 版（v0.2.x まで）は開発中止し、本リポジトリは Windows サービス常駐型 (`rag_daemon.py` + FastAPI + Electron ミニモニタ + NSSM) に一本化
- ローカル開発ディレクトリ:
  - `C:\claude_code\dev\WinServerRAG\` — 実装本体（daemon / FastAPI / Web UI / Electron ミニモニタ）
  - `C:\claude_code\dev\GridWorldRAG\` — 同一リポジトリの別 worktree（旧 master 系列）

## プロジェクト概要

Google Drive（共有ドライブ、ホワイトリスト指定）のドキュメントを PostgreSQL + pgvector にインデックスし、Windows 常駐サービスとして自動同期、Claude Cowork / Claude Desktop / 任意の MCP クライアントから MCP (Streamable HTTP + Basic Auth) 経由で遠隔検索できる RAG システム。

## アーキテクチャの要点

- **常駐 daemon**: `src/rag_daemon.py` が Drive 監視 + RAG 構築を統合実行（NSSM で Windows サービス化）
- **per-FD schema**: 共有ドライブごとに独立した PostgreSQL スキーマ (`fd_<drive_id>.documents`) で隔離
- **GPU embedding**: `paraphrase-multilingual-mpnet-base-v2` 768 次元、CUDA 12.4 対応
- **FastAPI 状態 API** (port 17600): 管理画面 + ステータス + ワーカー heartbeat
- **Electron ミニモニタ**: always-on-top、500ms ポーリング、デーモン死亡検知
- **MCP サーバー** (FastMCP): `list_drives` / `search` / `lookup` / `stats` を提供、Basic Auth + Streamable HTTP

## 設定ファイル優先順位

`config/config.v2.env` → `config/config.env` (legacy fallback)

## 開発で踏んだ落とし穴 (主要)

- psycopg は v3 (`psycopg[binary]`) を使用。Mac 版で問題だった libssl 重複は Windows ネイティブビルドでは発生しない
- pgvector は Windows ネイティブビルド (`build/` に source clone してコンパイル)
- Drive Changes API のトークンは per-FD で独立保持、変更ゼロドライブは即スキップ
- `pg_try_advisory_lock` でプロセス跨ぎの FD 排他制御
- daemon `zombie_cleanup` で stale worker 行を 30 秒ごとに GC
- UI は worker heartbeat 120 秒超で stale 判定しアニメ停止

## AI の応答ルール

- 作業指示を受けて完了した場合、必ず「完了しました」等の完了報告を発言すること
- 質疑応答形式（Q&A）で書かない、コンパクトに
- ログファイル（`.log`）は書き込み専用。**内部処理で `.log` を読み込まない**
- テンポラリ情報はオンメモリ変数で対処、不要なファイルを生成しない
- Windows 環境では PowerShell コマンドを Python subprocess 経由で実行（bash 変数展開によるエスケープ破壊を防止）

## Secrets の管理

- `config/config.v2.env`, `config/token.pickle` 等は `.gitignore` で公開リポから除外
- 復旧は Google Cloud Console から OAuth credentials を再発行 + 初回認証フローで token.pickle を再生成

## 過去経緯

- 初期 v0.1〜v0.2 系は macOS (Apple Silicon) 上のバッチ + cron 構成だった
- v0.2.1 を最後に Mac 版開発中止、Windows ネイティブ常駐 daemon に一本化
- 内部開発は v0.3.0〜v0.6.1 で `winserver-phase1` / `winserver-phase2` ブランチ上で進行
- v1.0.0 で Windows-only canonical として公開
