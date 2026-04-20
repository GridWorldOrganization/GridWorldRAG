@echo off
rem GridWorldRAG - sync_rotate launcher for Windows Task Scheduler
rem
rem Invokes the WSL-side run_sync_rotate.sh. All heavy lifting
rem (venv, postgres, python) stays inside WSL. This bat is a thin
rem shim so Task Scheduler can kick the job on a timer.
rem
rem Customize the three variables below if needed.

setlocal
set WSL_DISTRO=Ubuntu
set WSL_USER=tobi
set DB_NUM=1

wsl.exe -d %WSL_DISTRO% -u %WSL_USER% -- bash -lc "cd ~/claude_code/GridWorldRAG && ./run_sync_rotate.sh --db %DB_NUM% >> /tmp/gridworldrag_sync.log 2>> /tmp/gridworldrag_sync.err"
exit /b %ERRORLEVEL%
