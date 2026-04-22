@echo off
set PYTHONUTF8=1
set PYTHONPATH=C:\claude_code\dev\WinServerRAG
cd /d C:\claude_code\dev\WinServerRAG
"C:\claude_code\dev\WinServerRAG\.venv\Scripts\python.exe" -m src.aws_bridge >> "C:\Users\tobis\AppData\Local\WinServerRAG\logs\aws_bridge.log" 2>&1
