# Windows サービス化 (NSSM)

開発中は `run_api.bat` / `run_daemon.bat` を別々のコンソールで手動実行する。
運用時は NSSM (Non-Sucking Service Manager) で常駐サービス化する。

## NSSM 入手

```powershell
winget install NSSM.NSSM
# または
choco install nssm
```

## サービス登録 (管理者 PowerShell)

```powershell
$ROOT = "C:\claude_code\dev\WinServerRAG"

nssm install WinServerRAG-API "$ROOT\.venv\Scripts\python.exe" "-m" "src.control_api"
nssm set    WinServerRAG-API AppDirectory "$ROOT"
nssm set    WinServerRAG-API AppEnvironmentExtra PYTHONUTF8=1 PYTHONIOENCODING=utf-8
nssm set    WinServerRAG-API Start SERVICE_AUTO_START
nssm set    WinServerRAG-API AppStdout "$ROOT\logs\api.stdout.log"
nssm set    WinServerRAG-API AppStderr "$ROOT\logs\api.stderr.log"
nssm set    WinServerRAG-API AppStopMethodConsole 10000

nssm install WinServerRAG-Daemon "$ROOT\.venv\Scripts\python.exe" "-m" "src.rag_daemon"
nssm set    WinServerRAG-Daemon AppDirectory "$ROOT"
nssm set    WinServerRAG-Daemon AppEnvironmentExtra PYTHONUTF8=1 PYTHONIOENCODING=utf-8
nssm set    WinServerRAG-Daemon Start SERVICE_AUTO_START
nssm set    WinServerRAG-Daemon AppStdout "$ROOT\logs\daemon.stdout.log"
nssm set    WinServerRAG-Daemon AppStderr "$ROOT\logs\daemon.stderr.log"
nssm set    WinServerRAG-Daemon AppStopMethodConsole 30000

Start-Service WinServerRAG-API
Start-Service WinServerRAG-Daemon
```

## 解除

```powershell
Stop-Service WinServerRAG-API, WinServerRAG-Daemon
nssm remove WinServerRAG-API confirm
nssm remove WinServerRAG-Daemon confirm
```
