@echo off
setlocal
rem Run the RAG daemon on Windows.

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
rem Limit each worker's OpenMP pool to 1 so N=4 workers together use N CPU
rem cores, not N*N. Critical for CPU-only embedding under parallel load.
set OMP_NUM_THREADS=1
set MKL_NUM_THREADS=1

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
