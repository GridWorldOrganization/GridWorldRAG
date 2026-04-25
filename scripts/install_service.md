# サービス運用メモ

## ポリシー: Task Scheduler 禁止

本プロジェクトでは Windows タスクスケジューラへの登録は行わない。
バックアップも自動起動も、タスクスケジューラ経由では組まない。

- 理由: バックグラウンドで勝手に PowerShell / cmd が起動する挙動を避けるため。
- 代替: 必要なものは手動起動、または NSSM による Windows サービス化のみ。

## 起動プロセス一覧

実運用では 3 本のプロセスを常駐させる:

1. `src.control_api` — Web UI + REST API（ポート `API_PORT`、デフォルト 17600）
2. `src.rag_daemon` — インデックス構築デーモン（4 並列ワーカー）
3. `src.aws_bridge` — SQS/DDB ブリッジ（リモート Cowork からの MCP クエリ中継）

## バックアップ

`scripts/backup.bat` を手動実行して下さい。自動化はしません。

- 保持: 日次 7 世代、週次 (日曜) 4 世代
- 保存先: `backups/daily/*.dump` と `backups/weekly/*.dump`
- 復元: `pg_restore -U postgres -d winserverrag --clean --if-exists <file>.dump`
- 保存先変更: `WINSRV_BACKUP_DIR` 環境変数で上書き

## 手動起動（開発・実験時）

別々のターミナルで各プロセスを起動:

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

## NSSM (Non-Sucking Service Manager) による常駐化

管理者権限がある場合のみ推奨。タスクスケジューラではなく Windows サービス
として登録する。

### 入手

```powershell
# 公式 zip を手動展開するか:
winget install NSSM.NSSM
# または
choco install nssm
```

### サービス登録（管理者 PowerShell）

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
