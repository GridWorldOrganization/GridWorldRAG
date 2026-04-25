@echo off
setlocal
rem Launch the Electron mini monitor.
cd /d "%~dp0\..\desktop"
rem If API is on a different host/port, set WINSERVERRAG_API_URL here.
rem set "WINSERVERRAG_API_URL=http://127.0.0.1:17600"
call npm start
