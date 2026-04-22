# Changelog

All notable changes to WinServerRAG. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [v0.6.0] — 2026-04-22

**Theme: GPU 加速 + UI 磨き + 公開準備**

リランカーと埋め込みを CPU から NVIDIA GPU (CUDA 12.4) に移行、
warm 検索が **3–13 秒 → 840ms** (4〜15倍高速化)。同時に Web UI / Electron
ミニモニターのチラつき修正、セキュリティ強化、GridJapan 営業素材の整備、
そして GitHub 公開のための資格情報 scrub を済ませた区切り。

### Added
- **GPU サポート**: `torch==2.6.0+cu124` に入れ替え、`_pick_device()` が
  CUDA を自動検出。Akasaka PC (RTX 4070 SUPER) で VRAM 2.7–7 GB 使用、
  ピーク 78% GPU 使用率を実測。
- **Web UI GPU バッジ**: `/api/stats.device` に `kind/name/util/vram/power`
  を出し、stats-bar に「🎮 RTX 4070 SUPER · 11% · 6.9/12.0GB · 5W」形式
  で 5 秒おきに更新表示。CPU 環境では「💻 CPU」にフォールバック。
- **ミニモニター 2 段目プログレスバー**: 全 ON 済みドライブ合計の
  `total_files / ~enabled_files_estimate` を表示。
- **ドライブ別ファイル数プレビュー**: `fd_registry.file_count_estimate` +
  `file_count_estimate_at` カラム追加。起動時 + 30 分おきに全 29 ドライブ
  (ON/OFF 問わず) を軽量 API (`fields=files(id)`) でカウントしキャッシュ。
  ビルド ON にする前に「このドライブは〜20,082 件」と見える。
- **手動カウント再取得 API**: `POST /api/fds/{drive_id}/refresh-count`。
- **AWS サーバーレスブリッジ** (v0.5 で入った機能を v0.6 で公開状態に): 
  API Gateway + Lambda + SQS + DynamoDB + `src/aws_bridge.py` で、
  リモート Claude Cowork から MCP 検索可能。月額約 $0.10 (AWS 無料枠内)。
- **マーケ向けドキュメント**: `README.md` に GridJapan 営業ヘッダー、
  `docs/EVOLUTION.md` で v0.1 → v0.6 の進化の物語。

### Changed
- **ミニモニターポーリング**: 500ms → **250ms** (hash-gate でチラつき防止)。
- **Sheets API セマフォ**: `extract_spreadsheet_sheets()` が関数全体で
  Semaphore(2) を保持していたのを、**個別 HTTP 呼び出し単位に縮小**。
  30-sheet 級のスプレッドシートが他 2 ワーカーを 30 秒ブロックする事態を解消。
- **ワーカー進捗カウンタ**: `_handle_file` が `files_done` をインクリメント
  するよう修正。`total_files` は list-task 担当ワーカーだけでなく全員に
  `fd_registry.total_files_listed` から配布 (RC1)。
- **Delta 同期選択バグ修正**: `db.list_fds()` の SELECT に `rotate_token` を
  追加。以前は毎サイクル full rebuild ループに陥っていた (RC2)。
- **Terraform 変数**: `basic_user` / `basic_pass` のデフォルト値を削除
  (明示的な `terraform.tfvars` 指定が必須に)。
- **MCP seed users**: `db.seed_default_mcp_users()` が環境変数
  `WINSERVERRAG_SEED_USERS="user1:pw1,user2:pw2"` を読むように変更、
  バンドル `tobisako/admin` `izumi/admin` は削除。
- **ドキュメント**: `MCP.md` / `README.md` から初期パスワード `admin` の
  言及を削除、環境変数 seed に差し替え。

### Security
- **公開前の資格情報 scrub**: `git-filter-repo` で全履歴から
  `tobisako:admin` (+ base64 形式) / Lambda API Gateway ホスト名を置換。
  Trivy `fs` + `repo` スキャン合格 (secret=0, vuln=0)。
- **Lambda Basic Auth パスワードローテート**: 旧値 `admin` は 401、
  新 24 文字ランダム値で運用。
- **SQS サーバーサイド暗号化**: AWS-managed SSE を有効化 (Trivy AWS-0096)。
- **GitHub Secret Scanning + Push Protection** をリポで有効化。

### Removed
- `scripts/register_bridge_task.ps1` / `scripts/run_aws_bridge.bat`
  (Task Scheduler 登録スクリプトは方針上禁止に)。

### Fixed
- **Web 監視のワーカー表示がチラつく問題**: 差分ハッシュが `heartbeat_at`
  を含んでいて毎ポーリング再描画 → 表示項目だけをハッシュする
  `workersRenderHash()` に分離 (web + mini の両方)。
- **プログレスバーの二重計数バグ**: 4 ワーカー × 各 `total_files=221` を
  合計すると 884/884 を表示していた (実際は 221/221)。drive_id でグループ化
  した算出に修正 (`desktop/renderer.js`)。
- **MCP 初期化タイムアウト**: Lambda `MAX_WAIT_SEC` を 55 → 28 秒に下げて
  API Gateway 30 秒制限と整合。

### Meta
- `control_api.py` の FastAPI バージョンを `0.3.0` → `0.6.0` に更新。
- `CHANGELOG.md` を起こした (このファイル)。

**実測**
- warm search: **840 ms** (AWS 往復 + reranker 込み)
- cold search: 17.7 秒 (初回モデルロード)
- フルビルド 221 files: 5 分 (GPU) / 6-8 分 (CPU)
- 月額 AWS 運用コスト: ~$0.10 (10 queries/日)

## [v0.5.0] — 2026-04-22

**Theme: AWS サーバーレス経由で遠隔 MCP 接続**

Claude Cowork が別 PC (Hirai office) から Akasaka の RAG に接続できるよう、
AWS でパイプを構築。ビルド処理は AWS を一切触らず、クエリ時のみ中継が走る。

### Added
- `infra/aws/` (Terraform): API Gateway HTTP API + Lambda `mcp_handler` +
  SQS request queue + DynamoDB response table + `winserverrag-bridge`
  IAM user (最小権限)。
- `src/aws_bridge.py`: Akasaka 側の常駐 SQS long-poll ワーカー。
  `initialize` / `tools/list` / `tools/call` を既存の FastMCP ツールに
  ディスパッチ、結果を DynamoDB 経由で Lambda に返す。
- `tests/eval/_aws_tunnel_search.py`: AWS URL 経由の E2E テスト。

### Architecture
```
Hirai PC Cowork
      │ HTTPS POST /mcp (Basic Auth)
      ▼
AWS API Gateway → Lambda → SQS
                              ▲
                              │ long-poll
                         Akasaka PC aws_bridge
                              │ process
                              ▼
                        DynamoDB ← Lambda polls
```

## [v0.4.0] — 2026-04 (initial baseline)

**Theme: ファイル並列タスクキュー デーモン**

- 4 並列ワーカー + shared queue.Queue + `pg_try_advisory_lock(drive_id)`
- `fd_registry.pending_rotate_token` で worker 死亡時の復旧
- cancel_requested で即時停止 (enabled→disabled のエッジで)
- Web モニター 2 タブ (ビルド + MCP 検索スコープ)
- Electron ミニモニター (500ms ポーリング)
- FastMCP server (search / list_drives / lookup / stats)

## [v0.3.0] — (pre-publish)

- Quality Loop: `mcp_query_log` テーブル、eval suite (`tests/eval/`)
- search_enabled をグローバル化、per-user 廃止

## [v0.2.0] — (pre-publish)

- マルチスレッド化、MCP server 初実装、2-tab Web UI、Electron ミニ初版

## [v0.1.0] — (pre-publish)

- Phase 1: バッチ起動の `build_parallel.py` + Windows タスクスケジューラ

---

**Note**: v0.1 〜 v0.3 はこのリポジトリに入る前の開発記録で、
git 履歴としては保存されていません (別の monorepo の `winserver/`
サブディレクトリにありました)。v0.4 baseline からがこのリポジトリの開始点。
