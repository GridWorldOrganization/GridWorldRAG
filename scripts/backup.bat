@echo off
rem WinServerRAG — pg_dump backup with rotation.
rem Daily: keep last 7. Weekly (Sunday): keep last 4.
rem Run via Task Scheduler nightly (see install_service.md for setup).
setlocal enabledelayedexpansion

set "PG_BIN=C:\Program Files\PostgreSQL\17\bin"
set "PGHOST=localhost"
set "PGPORT=5432"
set "PGUSER=postgres"
if not defined PGPASSWORD set "PGPASSWORD=winserverrag"
set "PGDATABASE=winserverrag"

rem Backup directory (override via env var if needed)
if not defined WINSRV_BACKUP_DIR set "WINSRV_BACKUP_DIR=%~dp0\..\backups"
if not exist "%WINSRV_BACKUP_DIR%" mkdir "%WINSRV_BACKUP_DIR%"
if not exist "%WINSRV_BACKUP_DIR%\daily"  mkdir "%WINSRV_BACKUP_DIR%\daily"
if not exist "%WINSRV_BACKUP_DIR%\weekly" mkdir "%WINSRV_BACKUP_DIR%\weekly"

rem Build timestamp
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value ^| findstr "="') do set "DT=%%I"
set "YYYY=%DT:~0,4%"
set "MM=%DT:~4,2%"
set "DD=%DT:~6,2%"
set "HH=%DT:~8,2%"
set "Mi=%DT:~10,2%"
set "STAMP=%YYYY%-%MM%-%DD%_%HH%%Mi%"

rem Weekday (Sunday=0 or depending on locale — check via PS)
for /f %%I in ('powershell -NoProfile -Command "(Get-Date).DayOfWeek.Value__"') do set "DOW=%%I"

set "DAILY=%WINSRV_BACKUP_DIR%\daily\winserverrag_%STAMP%.dump"
set "WEEKLY=%WINSRV_BACKUP_DIR%\weekly\winserverrag_%STAMP%.dump"

echo [backup] starting pg_dump to %DAILY%
"%PG_BIN%\pg_dump.exe" -h %PGHOST% -p %PGPORT% -U %PGUSER% -F c -Z 6 -f "%DAILY%" %PGDATABASE%
if errorlevel 1 (
  echo [backup] pg_dump FAILED
  exit /b 1
)
echo [backup] OK

rem Copy to weekly on Sunday (DOW=0)
if "%DOW%"=="0" (
  copy /Y "%DAILY%" "%WEEKLY%" >nul
  echo [backup] weekly snapshot copied
)

rem Retention: keep 7 most recent daily and 4 most recent weekly.
powershell -NoProfile -Command "Get-ChildItem '%WINSRV_BACKUP_DIR%\daily\*.dump' | Sort-Object LastWriteTime -Descending | Select-Object -Skip 7 | Remove-Item -Force"
powershell -NoProfile -Command "Get-ChildItem '%WINSRV_BACKUP_DIR%\weekly\*.dump' | Sort-Object LastWriteTime -Descending | Select-Object -Skip 4 | Remove-Item -Force"

echo [backup] retention applied
endlocal
exit /b 0
