# Register WinServerRAG AWS bridge as a scheduled task that runs at user logon.
# Runs under the current user (tobisako) so it can access ~/.aws credentials + venv.
# No admin elevation required.
#
# Usage:
#   pwsh -File scripts\register_bridge_task.ps1
# Uninstall:
#   schtasks /Delete /TN "WinServerRAG-AWSBridge" /F

$TaskName = "WinServerRAG-AWSBridge"
$Repo     = "C:\claude_code\dev\WinServerRAG"
$Python   = "$Repo\.venv\Scripts\python.exe"
$LogDir   = "$env:USERPROFILE\AppData\Local\WinServerRAG\logs"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Launcher batch: sets UTF-8, PYTHONPATH, redirects stdout/stderr to rotating log.
$Launcher = @"
@echo off
set PYTHONUTF8=1
set PYTHONPATH=$Repo
cd /d $Repo
"$Python" -m src.aws_bridge >> "$LogDir\aws_bridge.log" 2>&1
"@
$LauncherPath = "$Repo\scripts\run_aws_bridge.bat"
Set-Content -Path $LauncherPath -Value $Launcher -Encoding ASCII
Write-Host "Launcher written: $LauncherPath"

# Action: run the launcher.
$action = New-ScheduledTaskAction -Execute $LauncherPath

# Trigger: at current user logon (admin not required).
# NOTE: -AtStartup needs elevation; rely on auto-login for always-on PC.
$trig1 = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Settings: auto-restart on failure, no time limit, run hidden.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 365) `
    -MultipleInstances IgnoreNew

# Principal: run as current user, interactive.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

# Register (Force replaces if already exists).
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trig1 `
    -Settings $settings `
    -Principal $principal `
    -Description "WinServerRAG AWS SQS/DDB bridge (MCP pipe for remote Cowork)" `
    -Force | Out-Null

Write-Host "Task registered: $TaskName"
Write-Host "Log directory:   $LogDir"
Write-Host ""
Write-Host "Commands:"
Write-Host "  Start:   schtasks /Run    /TN $TaskName"
Write-Host "  Stop:    schtasks /End    /TN $TaskName"
Write-Host "  Status:  schtasks /Query  /TN $TaskName /V /FO LIST"
Write-Host "  Remove:  schtasks /Delete /TN $TaskName /F"
