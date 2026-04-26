# サービス運用メモ

## 推奨: インストーラー経由 (v1.2.0+)

```
dist\WinServerRAG-Setup-1.2.0.exe
```

を管理者として実行すれば、以下が自動で行われます:

- exe 4 本 (`winserverrag-api`, `winserverrag-daemon`, `winserverrag-dbinit`,
  `winserverrag-backup`) + Electron ミニモニタを `%ProgramFiles%\WinServerRAG\` に配置
- NSSM による Windows サービス 2 本登録 + auto-start:
  - `WinServerRAG-API` (port 17600)
  - `WinServerRAG-Daemon` (`DependOnService = WinServerRAG-API`)
- ログローテーション (10MB 上限)、`%ProgramData%\WinServerRAG\logs\` 配下
- スタートメニューにミニモニタ + Web Console ショートカット
- アンインストール時に上記 2 サービスを stop + remove

ビルド方法は [installer/README.md](../installer/README.md) 参照。

## ポリシー: Task Scheduler 禁止

本プロジェクトでは Windows タスクスケジューラへの登録は行わない。
バックアップも自動起動も、タスクスケジューラ経由では組まない。

- 理由: バックグラウンドで勝手に PowerShell / cmd が起動する挙動を避けるため。
- 代替: NSSM による Windows サービス化のみ (上記インストーラーが自動で行う)。

## 起動プロセス一覧 (本番)

インストーラーが登録する 2 サービス:

1. `WinServerRAG-API` — Web UI + REST API (port `API_PORT`、デフォルト 17600)
2. `WinServerRAG-Daemon` — インデックス構築デーモン (4 並列ワーカー)

オプション: `aws_bridge` (リモート Cowork 接続) は v1.2 インストーラーでは
未登録。必要な場合は下記「手動 NSSM 登録」のセクションで追加してください。
v1.3+ で自動登録予定。

## バックアップ

インストーラー配下では `winserverrag-backup.exe` を手動実行:

```powershell
& "C:\Program Files\WinServerRAG\bin\winserverrag-backup\winserverrag-backup.exe"
```

- 保持: 日次 7 世代、週次 (日曜) 4 世代
- 保存先: `%ProgramData%\WinServerRAG\backups\daily\*.dump` と
  `%ProgramData%\WinServerRAG\backups\weekly\*.dump`
- 復元: `pg_restore -U postgres -d winserverrag --clean --if-exists <file>.dump`
- 保存先変更: `WINSRV_BACKUP_DIR` 環境変数で上書き

## 開発時の手動起動

開発機では venv 経由で直接起動するのが楽 (再ビルド不要):

```powershell
cd C:\claude_code\dev\WinServerRAG

# Terminal 1: Web UI / API
.venv\Scripts\python.exe -m src.control_api

# Terminal 2: Daemon
.venv\Scripts\python.exe -m src.rag_daemon

# Terminal 3: AWS bridge (リモート Cowork 接続が必要な場合のみ)
.venv\Scripts\python.exe -m src.aws_bridge
```

各ターミナルは開いたままにする。閉じるとプロセスが停止する。

## 手動 NSSM 登録 (インストーラーを使わない場合)

管理者権限がある場合のみ推奨。インストーラーが行うのと同じことを手動で。

### 入手

```powershell
# 公式 zip を手動展開するか:
winget install NSSM.NSSM
# または
choco install nssm
```

### サービス登録 (管理者 PowerShell)

```powershell
$ROOT = "C:\claude_code\dev\WinServerRAG"

# API
nssm install WinServerRAG-API "$ROOT\.venv\Scripts\python.exe" "-m" "src.control_api"
nssm set    WinServerRAG-API AppDirectory "$ROOT"
nssm set    WinServerRAG-API AppEnvironmentExtra PYTHONUTF8=1 PYTHONIOENCODING=utf-8
nssm set    WinServerRAG-API Start SERVICE_AUTO_START
nssm set    WinServerRAG-API AppStdout "$ROOT\logs\api.stdout.log"
nssm set    WinServerRAG-API AppStderr "$ROOT\logs\api.stderr.log"
nssm set    WinServerRAG-API AppStopMethodConsole 10000

# Daemon
nssm install WinServerRAG-Daemon "$ROOT\.venv\Scripts\python.exe" "-m" "src.rag_daemon"
nssm set    WinServerRAG-Daemon AppDirectory "$ROOT"
nssm set    WinServerRAG-Daemon AppEnvironmentExtra PYTHONUTF8=1 PYTHONIOENCODING=utf-8
nssm set    WinServerRAG-Daemon Start SERVICE_AUTO_START
nssm set    WinServerRAG-Daemon AppStdout "$ROOT\logs\daemon.stdout.log"
nssm set    WinServerRAG-Daemon AppStderr "$ROOT\logs\daemon.stderr.log"
nssm set    WinServerRAG-Daemon AppStopMethodConsole 30000

# AWS bridge (リモート Cowork 接続用、必要なら)
nssm install WinServerRAG-Bridge "$ROOT\.venv\Scripts\python.exe" "-m" "src.aws_bridge"
nssm set    WinServerRAG-Bridge AppDirectory "$ROOT"
nssm set    WinServerRAG-Bridge AppEnvironmentExtra PYTHONUTF8=1 PYTHONIOENCODING=utf-8
nssm set    WinServerRAG-Bridge Start SERVICE_AUTO_START
nssm set    WinServerRAG-Bridge AppStdout "$ROOT\logs\bridge.stdout.log"
nssm set    WinServerRAG-Bridge AppStderr "$ROOT\logs\bridge.stderr.log"

Start-Service WinServerRAG-API, WinServerRAG-Daemon, WinServerRAG-Bridge
```

### 解除

```powershell
Stop-Service   WinServerRAG-API, WinServerRAG-Daemon, WinServerRAG-Bridge
nssm remove    WinServerRAG-API    confirm
nssm remove    WinServerRAG-Daemon confirm
nssm remove    WinServerRAG-Bridge confirm
```
