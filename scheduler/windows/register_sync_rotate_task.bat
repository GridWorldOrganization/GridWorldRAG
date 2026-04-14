@echo off
rem GridWorldRAG - register sync_rotate as Windows Scheduled Task
rem
rem Creates "GridWorldRAG-Sync" scheduled task that runs every 1 minute,
rem hidden, single-instance, while the user is logged on.
rem
rem No admin privileges required (task is registered under current user).
rem Re-run to update; /Force overwrites existing task.

setlocal
set TASK_NAME=GridWorldRAG-Sync
set SCRIPT_DIR=%~dp0
set RUN_BAT=%SCRIPT_DIR%run_sync_rotate.bat

if not exist "%RUN_BAT%" (
    echo [ERROR] %RUN_BAT% not found
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$action = New-ScheduledTaskAction -Execute '%RUN_BAT%';" ^
  "$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650);" ^
  "$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -StartWhenAvailable;" ^
  "$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited;" ^
  "Register-ScheduledTask -TaskName '%TASK_NAME%' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'GridWorldRAG rotation-style incremental sync' -Force"

if %ERRORLEVEL% neq 0 (
    echo [ERROR] Task registration failed
    exit /b 1
)

echo.
echo Registered: %TASK_NAME%  (interval: 1 minute, hidden, single-instance)
echo View:   schtasks /query /tn %TASK_NAME% /v /fo list
echo Remove: scheduler\windows\unregister_sync_rotate_task.bat
echo Logs:   \\wsl.localhost\Ubuntu\tmp\gridworldrag_sync.log
exit /b 0
