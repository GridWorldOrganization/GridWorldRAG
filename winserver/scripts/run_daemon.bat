@echo off
setlocal
rem Run the RAG daemon on Windows.

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

cd /d "%~dp0\.."

if not exist ".venv\Scripts\python.exe" (
  echo [run_daemon] .venv not found, creating...
  python -m venv .venv || goto :err
  call .venv\Scripts\activate.bat
  pip install -U pip || goto :err
  pip install -r requirements.txt || goto :err
) else (
  call .venv\Scripts\activate.bat
)

python -m src.rag_daemon
goto :eof

:err
echo [run_daemon] setup failed
exit /b 1
