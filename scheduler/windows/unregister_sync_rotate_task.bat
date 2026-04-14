@echo off
rem GridWorldRAG - remove the GridWorldRAG-Sync scheduled task

schtasks /delete /tn "GridWorldRAG-Sync" /f
exit /b %ERRORLEVEL%
