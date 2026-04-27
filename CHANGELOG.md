# Changelog

All notable changes to GridWorldRAG (Windows canonical). Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [v1.4.0] — 2026-04-28

**Theme: インストーラ→ウィザード→サービス起動の連鎖を完走可能に**

v1.3.0 でセットアップ可視化までは整ったが、実環境で初回起動を試すと
API サービスが PAUSED 無限ループ、daemon が worker を再生成しない、
ウィザードの PG 接続テストがパスワード未入力でも成功してしまう、と
3 系統で連鎖が止まっていた。本 release は 3 系統まとめて修正、新規
PC のクリーン環境でセットアップを完走できる状態にする。

### Fixed

- **API サービスが NSSM PAUSED 無限ループに陥るバグ** (`src/control_api.py`)
  - `uvicorn.run("src.control_api:app", ...)` の文字列インポート形式が
    PyInstaller frozen exe で破綻。エントリースクリプトは `__main__`
    として読み込まれるため `importlib.import_module("src.control_api")`
    が失敗し、`ERROR: Error loading ASGI app. Could not import module
    "src.control_api"` を吐いて即終了 → NSSM が指数バックオフで再試行
    → 5 回失敗で PAUSED 固定。`uvicorn.run(app, ...)` でオブジェクト
    直渡しに変更。
  - `_lifespan` の Drive pre-auth が `token.pickle` 不在時に
    `flow.run_local_server` の OAuth 待機で永遠にブロックする問題。
    token 存在チェックを先に入れて、無ければ pre-auth スキップ +
    遅延認証フォールバックに統一。
- **daemon が stale worker を検出しても再生成しないバグ** (`src/rag_daemon.py`)
  - `db.cleanup_zombies()` は `daemon_workers` の DB 行を削除して
    `workers_removed=[wid]` を返すが、daemon の in-process `_workers`
    dict (スレッド参照) は更新していなかった。`_live_worker_count() =
    len(_workers)` が target を維持し続けるため、スタック中の worker
    が消えても respawn 条件 `_live_worker_count() < target`
    (rag_daemon.py:721) が成立しない。zombie cleanup 直後に
    `_workers.pop(wid, None)` を追加。Python はスレッドを強制終了
    できないので blocked thread は残るが、新しい worker が立ち上がり
    キュー消化が再開する。
- **ウィザード PG 接続テストの認証バイパス** (`desktop/wizard-ipc.js`)
  - PR #56 で `net.connect` TCP 確認に切り替えたが「PW 未入力でも
    成功」する状態だった。`pg` npm パッケージで wire-protocol レベル
    の `pg.Client.connect()` に置換 — TCP + StartupMessage + SCRAM-
    SHA-256 + `SELECT 1` まで完走させて初めて ✅ を返す。失敗時は
    SQLSTATE → 英語固定文の map で表示 (cp932 mojibake 回避)。
  - dev mode (`npm start`) で `winserverrag-dbinit.exe` が存在せず
    Step 6 が失敗する問題。`resolveDaemonHelper()` で
    `<repo>\.venv\Scripts\python.exe -m src.db_init` にフォールバック。

### Changed

- ウィザード Step 2 PG パスワード欄に **👁 表示/非表示トグル** と
  **文字数カウンタ** を追加 (実ユーザーが 12 文字目標で 13 文字
  打ってしまい、マスク表示で typo に気付けなかった事例の対応)
- ウィザード `go()` が step 遷移時に `setStatus("")` を打つ — 直前
  step の "接続 OK" がフッターに残って次 step を汚染するのを防止
- ウィザード PG 接続テストボタン文言: "接続テスト (TCP)" → "接続
  テスト (認証込み)"。実態を反映

### Reviewed via

- 実環境クリーン install テスト (Microsoft Account `tobisako@gridworld.co`)
- `_full_restart.ps1` で daemon+API 再デプロイ → `target=2 live=2` を確認
- `/api/stats` 完全疎通: 30 FDs / 4800 files / pg_ok=true / drive_ok=true / GPU 検出

### Migration

なし。`token.pickle` 既存環境は従来通り起動時 pre-auth が走る。
インストーラ既知ユーザは `Program Files\WinServerRAG\bin\` の
`winserverrag-api\` と `winserverrag-daemon\` を v1.4.0 ビルドの
`dist/exe/` から差し替え。NSSM 設定は変更なし。

## [v1.3.0] — 2026-04-26

**Theme: ミニモニタを daemon SCM 直結に格上げ — API 不通でも状態が見える**

ミニモニタが API のライフサイクルから独立。daemon の状態を Windows SCM
(`sc query`) から直接取得するため、API が落ちていても daemon の
「実行中 / 停止中 / 未インストール」が正しく表示される。停止中 daemon は
**UAC 不要で起動**できる (インストーラーが SDDL を緩和)。

### Added

- `desktop/service-control.js` (NEW): pure-Node ヘルパー。
  - `parseScQueryOutput()` — `STATE : N` の数値 parse (locale 非依存)
  - `queryService()` — Electron main から `%SystemRoot%\System32\sc.exe` 直叩き
  - `classifyStartExit()` — sc start exit code 細別 (0/5/1056/1060/1068/1053)
  - `startService()` — UAC なし起動 (SDDL 緩和済前提)
  - `detectDevMode()` — `.venv\Scripts\python.exe` 検出で dev mode 判定
- `desktop/__tests__/service-control.test.js` (NEW): Node native test 25 件
- `installer/install-services.ps1` (NEW): NSSM 登録 + SDDL 緩和の冪等スクリプト
  - `WinServerRAG Operators` ローカルグループ作成
  - インストーラー実行ユーザーをグループに add
  - SDDL `(A;;LCRP;;;<SID-of-Operators>)` で SERVICE_QUERY_STATUS +
    SERVICE_START のみ付与 (SERVICE_STOP は付与しない、AU でなく
    Operators — Microsoft 推奨パターン)
  - 既存サービスを検出して params のみ更新する真の冪等性
  - `-Uninstall` モードで stop + remove + group 削除

### Changed

- `desktop/main.js` + `desktop/preload.js`: IPC `daemon-status` (5s
  ポーリング) と `daemon-start` (▶ クリック時) を追加
- `desktop/renderer.js`:
  - **Daemon 行が API から独立**: `pollDaemon()` 5s ループで `sc query`
    結果を直接反映
  - Button state machine 拡張 (5 → 8 状態): running / paused /
    start_pending / stop_pending / continue_pending / pause_pending /
    paused / stopped / not_installed (dev|prod)
  - `showErr()` (API 接続失敗時) は build 行・統計のみクリア、
    daemon 行は触らない (v1.3 の主要な約束)
  - `showDevModeDialog()`: dev mode 検出時に `python -m src.rag_daemon`
    コマンドを Ctrl+C コピー可能な形で表示
  - `showNotInstalledDialog()`: 本番未インストール時にインストーラー
    実行を案内 (mini-monitor は **install しない**)
- `desktop/index.html`: row label "サービス" → "Daemon" (id 据置で
  renderer 側差分最小)
- `installer/inno/WinServerRAG.iss`: `[Run]` の長い NSSM コマンド列
  (~80 行) を `install-services.ps1` 1 invocation に置換。
  `[UninstallRun]` も同 PS1 -Uninstall に置換。`[Files]` で
  `install-services.ps1` を `{app}\bin\` へ配置

### Reviewed via

- `/plan-eng-review` (HOLD_SCOPE) — 4 sections、12 issues found+resolved
- `/codex review` (outside voice、ChatGPT 認証) — **12 findings 全採用**:
  - SDDL は AU でなく `WinServerRAG Operators` ローカルグループ
  - SERVICE_STOP は付与しない (button が stop しないため)
  - State 数値 parse (`STATE : 4`)、localized text 不依存
  - sc.exe フルパス使用 (PATH 非依存)
  - Pending states 独立表示 (黃 + spinner)
  - sc start error code 細別 (5/1056/1060/1068/1053)
  - NSSM install 冪等化 (detect+update、blind install しない)
  - 2-stream UI 合成 (showErr が daemon 行を触らない)

### Tests

- 既存 28 Python tests 全 pass (PR #28 の 7 件含む、後方互換)
- 新規 25 Node native tests 全 pass:
  - parseScQueryOutput: 12 cases (各 STATE 数値 + edge cases)
  - classifyStartExit: 7 cases (主要 exit code + unknown)
  - scExePath: 3 cases (env var + fallback)
  - detectDevMode: 2 cases
  - exports surface: 1 case

### Notes

- ミニモニタは **install / uninstall を絶対に行わない**。インストール
  は v1.2 インストーラーの責務。security boundary 永久
- v1.3 では path manifest / dev mode auto-spawn は scope 外
  (Codex 指摘で v1.4 候補に降格)
- pause/resume は引き続き API 経由 (v1.1)。Daemon SCM 操作は
  Electron main 直接

### PRs merged

- #29 docs: v1.3 mini-monitor ↔ daemon protocol spec
- #30 feat(mini-monitor): daemon SCM control + ACL relaxation

## [v1.2.0] — 2026-04-26

**Theme: 4 .exe + Inno Setup インストーラー (.bat → exe 移行)**

これまで `scripts/*.bat` で起動していた本機能を **PyInstaller で .exe 化** し、
**Inno Setup インストーラー** にまとめました。新規 PC への展開がワンクリックに。

### Added — exe pipeline
- `winserverrag-api.exe` (`src.control_api` の PyInstaller --onedir bundle)
- `winserverrag-daemon.exe` (`src.rag_daemon`、OMP/MKL 1 thread runtime hook 内蔵)
- `winserverrag-dbinit.exe` (`src.db_init`)
- `winserverrag-backup.exe` (新規 `src.db_backup` — `backup.bat` を Python に
  port、pg_dump シェルアウト + 7/4 世代ローテーション)
- ランタイムフック: UTF-8 強制 (`rt_utf8.py`) + OpenMP/MKL=1 (`rt_omp_threads.py`、
  daemon のみ)
- 共通 PyInstaller helper: `installer/pyinstaller/_common.py` で hidden imports /
  data files / excludes をシェア (sentence-transformers / fastapi / pgvector
  などの動的 import を補足)

### Added — Inno Setup installer (`installer/inno/WinServerRAG.iss`)
- `dist\WinServerRAG-Setup-1.2.0.exe` (~600MB) を出力
- `%ProgramFiles%\WinServerRAG\bin\` に 4 つの exe + `nssm.exe`
- `%ProgramFiles%\WinServerRAG\mini\` に Electron ミニモニタ
- `%ProgramData%\WinServerRAG\{config,logs,backups\daily,backups\weekly}\` 作成
  (権限: users read-exec、admins modify、logs/backups は users modify)
- NSSM 経由で Windows サービス 2 本登録 + auto-start:
  - `WinServerRAG-API`
  - `WinServerRAG-Daemon` (`DependOnService = WinServerRAG-API`)
  - 両方とも 10MB ローテーションログ
- スタートメニューショートカット (Mini Monitor、Web Console)
- アンインストーラーがサービス stop + remove を自動実行
- Pre-install チェック: PostgreSQL 17 サービス検出、不在時は警告 (中止可)

### Added — build pipeline (`installer/build.ps1`)
- 一発ビルド: PyInstaller × 4 → electron-builder → Inno Setup compile
- NSSM (2.24) 自動ダウンロード + キャッシュ
- スイッチ: `-CleanFirst` / `-SkipPython` / `-SkipElectron` / `-Version <X>`
- ビルド時間: cold ~7-10 分 (Akasaka PC)

### Changed
- `desktop/package.json`: `electron-builder` 25 を devDeps に追加、`pack` /
  `dist` スクリプト追加、`build` セクション (appId / productName / target=dir)
- `scripts/install_service.md`: インストーラー経由の手順を主推奨、手動 NSSM
  登録は dev / カスタマイズ用に残置

### Notes
- **CPU torch のみ bundle** (CUDA は ~3GB で配布対象「~500MB-1GB」に収まらない)。
  GPU 環境では post-install で `pip install torch==2.6.0 --index-url
  https://download.pytorch.org/whl/cu124` (READMEに記載)。Akasaka PC は
  既存 venv のまま (インストーラー経由デプロイは未実施)
- **コード署名なし** (内部運用のため)。Smart Screen 警告は許容
- **AWS bridge** (`src.aws_bridge`) は v1.2 インストーラー未登録。リモート
  Cowork 接続が必要な場合は手動 NSSM 登録 (install_service.md 参照)。
  v1.3+ で自動登録予定
- **PG / pgvector / NVIDIA driver は prereq**。インストーラーが pre-install
  で検出のみ

### Removed
- `scripts/run_api.bat` / `run_daemon.bat` / `run_mini.bat` / `backup.bat` /
  `db_init.bat` の 5 個 bat ファイル。dev 環境は `python -m src.X` (venv 経由)
  か `npm start` を直接呼ぶ。本番は exe 経由

### Build prereqs (ビルドマシン側)
- Python 3.12 + venv + PyInstaller (`pip install pyinstaller`)
- Node.js 20+ (electron-builder)
- Inno Setup 6 (<https://jrsoftware.org/isdl.php>) または `winget install JRSoftware.InnoSetup`
- NSSM 2.24 (build.ps1 が winget キャッシュから自動取得、または `winget install NSSM.NSSM`)
- Windows Developer Mode 有効化 (electron-builder の symlink 抽出に必要)

### 既知の制約 (v1.2.0)
- **bundle size**: 開発 venv に CUDA torch (3.6GB) が install されている場合、
  PyInstaller --onedir 出力が各 ~4GB に膨らむ (target 500MB-1GB 超過)。
  v1.2.1 で `.venv-build/` 別 venv (CPU torch のみ) 対応の `-BuildVenv` 切替を予定。
  当面は手動で `.venv-build` を作成して `installer/build.ps1` の
  `$VenvPython` を書き換える [installer/README.md](./installer/README.md) 参照
- **electron-builder symlink エラー**: Windows Developer Mode 未有効だと
  winCodeSign 抽出時に dylib symlink 作成失敗。一回 ON すれば以降 OK
- **nssm.cc 503**: build.ps1 は winget キャッシュ (`NSSM.NSSM`) を fallback。
  `winget install NSSM.NSSM` を一回実行すれば nssm.cc 不要

詳細: [installer/README.md](./installer/README.md)

## [v1.1.0] — 2026-04-26

**Theme: pause/resume daemon control + mini-monitor toggle button**

v1.0.0 の README で「実装は v1.1 で予定」と書いた pause/resume コントロール面を
実装。daemon の **新規タスク取得を一時停止/再開** できるようになり、ミニモニタ
に **⏸/▶ アイコンボタン** を配置。CI を Windows 11 matrix に拡張。

### Added
- `POST /api/daemon/pause` / `POST /api/daemon/resume` / `GET /api/daemon/state`
  (transition-only `since` で「停止してからの経過時間」表示が信頼できる)
- `/api/stats` レスポンスに `paused: bool` + `paused_since: ISO8601` を追加
- ミニモニタの ⏸/▶ アイコントグルボタン (360px header に収まる icon-only)
- paused 状態を `setIndicator` の独立 state に追加 (gray)、優先度
  `err > paused > active > ok > idle`
- 「停止中（手動停止）」ステータステキスト (README spec line 149 順守)
- `daemon_config['paused']` 単一 JSON 行で race-free 永続化、サービス再起動後も
  状態保持
- GitHub Actions の `Lint` workflow に `windows-latest` matrix を追加 (`shell:
  bash` で line-continuation を統一)

### Changed
- `_manager_iter` が pause 中 enqueue を skip。cancel-drain と finalize は継続
  (in-flight ビルドの commit を保証)
- Worker dispatch loop が pause 中 queue 残留 `list_full` / `list_delta` task を
  drop (defense-in-depth)。`file` / `file_delete` / `finalize` は常に処理
- `daemon_events.log_event()` 失敗を pause/resume API は try/except で吸収 (event
  table エラーで control plane を落とさない)
- `_stats_pump` が paused state も毎 5s 更新、加えて pause/resume API 内で
  `_stats_cache` を **同期更新** (ミニモニタ button 反映が即時)
- Lifespan startup で `_stats_cache.paused` を DB から bootstrap

### Cleanup (in CI matrix PR)
- F401 unused imports: `aws_bridge.logging`, `control_api.{Path,timezone}`,
  `embedding.Optional`, `mcp_auth.os`, `rag_daemon.Tuple`, `reranker.Optional`
- F811 `control_api.timezone` 二重 import (関数内 import のみ残す)
- F841 `db.UniqueViolation as e` の unused `e`
- E127 `api_set_user_scope` continuation indent
- E306 `api_refresh_count` 内 nested `def _work():` 前の空行

### Tests
- 21 新規テスト 全 pass:
  - `test_pause_helpers.py` (8): KV JSON、transition-only since、idempotency、
    persistence、malformed JSON degradation、ISO Z 接尾辞 parse
  - `test_control_api_pause.py` (6): endpoint flip state + cache push、
    idempotent preserve since、_try_log_event swallows、end-to-end consistency
  - `test_rag_daemon_pause.py` (7): manager skip enqueue when paused、normal
    flow when not paused、finalize/cancel still run when paused、paused-read
    failure handling、worker drop list_* only

### Reviewed via
- `/plan-eng-review` (HOLD_SCOPE) — 4 sections, 0 critical gaps
- `/codex review` (outside voice) — 14 findings, all adopted

## [v1.0.0] — 2026-04-26

**Theme: Windows-only canonical release — Mac line sunset**

`master` ブランチの tree を `winserver-phase2` (Windows 11 ネイティブ常駐
daemon の本番コード) と同一化。Mac 版 (v0.2.x) は開発中止、本リポジトリは
Windows 単一系統に一本化された。両 branch の commit history は merge commit
(parents: `ebd7dae` + `a3a6cb2`) で保全。

### BREAKING
- **Mac (macOS) サポート終了**。最終 Mac リリースは v0.2.1 (タグ参照)。
  以降のバグ修正・機能追加は行わない。

### Added (winserver-phase2 由来)
- 常駐 daemon `src/rag_daemon.py` — Drive 監視 + RAG 構築を統合実行
- FastAPI 状態 API + Web 管理画面 (port 17600)
- Electron ミニモニタ (always-on-top, 500ms 更新, debug overlay Ctrl+Shift+D)
- per-FD schema 分離 (`fd_<drive_id>.documents`)
- GPU embedding (CUDA 12.4, `paraphrase-multilingual-mpnet-base-v2`)
- AWS serverless MCP bridge (Basic Auth + Streamable HTTP)
- pause/resume API + ミニモニタ toggle button **設計** (実装は v1.1)
- NSSM Windows サービス化スクリプト (`scripts/install_service.md`)

### Removed (Mac 版コード)
- `build_parallel.py` / `build_single.py` / `sync_rotate.py`
- Mac 想定の `src/` モジュール群、`launchd/` plist、`scheduler/windows` (WSL2 経由)
- `monitor.sh` / `setup.sh` / `run_*.sh`
- `docs/mac-resident-daemon.md`、Mac 想定の `docs/architecture.md` 他
- `Dockerfile` / `docker-compose.yml` (Mac 開発環境用)
- `QUICKSTART.md` / `CONTRIBUTING.md` (Mac セットアップ前提)

### Preserved from old master
- LICENSE, SECURITY.md, CODE_OF_CONDUCT.md
- `.github/ISSUE_TEMPLATE/*`, `pull_request_template.md`, `CODEOWNERS`,
  `dependabot.yml`
- `.github/workflows/lint.yml`, `codeql.yml`
  (Windows-latest matrix 化は v1.1 で別 PR 予定)

### Pre-1.0 development (winserver-phase2 internal tags)
v0.3.0 〜 v0.6.1 は `winserver-phase2` ブランチ上の development tag であり、
public release は本 v1.0.0 にロールアップされる。下記は内部時系列の主要点:

- **v0.6.1** (2026-04-22): drive_client docstring rewrite、control_api → 0.6.1
- **v0.6.0** (2026-04-22): GPU embedding (CUDA 12.4)、warm search 3–13s → **840ms**
- **v0.5.x**: per-user MCP search scope、admin API security hardening
- **v0.4.x**: Electron mini-monitor、debug overlay、AWS serverless bridge
- **v0.3.x**: rag_daemon 統合、FastAPI 管理画面、per-FD schema 分離

### Migration
Mac 版 (v0.2.1) を運用していたユーザーは継続不可。Windows ネイティブ環境に
切替が必要 (PostgreSQL 17 + pgvector / Python 3.12 / NSSM)。詳細は README
クイックスタート参照。

## [v0.2.1] — 2026-04-21 (Mac line final)

[v0.2.1 tag](https://github.com/GridWorldOrganization/GridWorldRAG/releases/tag/v0.2.1)
で参照可能。Mac 版の最終リリース。以降の Mac 版開発は行わない。

---

# 以降は winserver-phase2 ブランチ上の development changelog (v1.0.0 にロールアップ済)

## [v0.6.1] — 2026-04-22

**Theme: 一本化 — Mac 側との二重構造を廃止、Windows 単線に**

v0.6.0 まで「Mac 版 GridWorldRAG (`master`) + Windows 版 WinServerRAG
(`winserver-phase2`)」の二系統で運用していたが、開発リソースを
Windows 側に集約することを決定。Mac 側は以後メンテナンスせず、
このリポジトリ・ブランチが **唯一の運用ライン** となる。

### Changed
- **README.md**: 「姉妹プロジェクト Mac 版」「既存 GridWorldRAG との違い」
  テーブルを削除。Windows 環境前提で書き直した。
- **drive_client.py**: "Ported from GridWorldRAG" のモジュール docstring を
  書き換え（現状の責務と呼び出し元を明記）。
- **廃案セクション**: "Mac リモートモニター" 表記を「リモートモニター別アプリ」
  に修正 (OS 中立化)。
- **control_api.py**: FastAPI app version を `0.6.0` → `0.6.1`。

### Removed
- **docs/PUSH_PLAN.md**: 初回公開時の意思決定ログ。v0.6.0 リリースで役目を
  終えたので削除（必要なら git 履歴に残存）。

### Meta
- **Mac 側コードベース (`master` branch)** の扱い: 以後 WinServerRAG 側では
  参照しない。Mac 側が生きているかどうかは今後別 org/repo へ移すか否かの
  判断に委ねる。今日時点では `master` を触らない (abandon-in-place)。

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
