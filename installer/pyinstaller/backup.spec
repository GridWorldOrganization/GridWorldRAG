# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — winserverrag-backup.exe

Wraps `python -m src.db_backup`. Pure stdlib + subprocess shell-out to
pg_dump.exe — no DB driver imports needed, so the spec is much smaller
than api/daemon.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(SPECPATH).resolve().parent.parent))
from installer.pyinstaller._common import COMMON_EXCLUDES  # noqa: E402


PROJECT_ROOT = str(Path(SPECPATH).resolve().parent.parent)
ENTRY = str(Path(PROJECT_ROOT) / "src" / "db_backup.py")
HOOKS = [
    str(Path(SPECPATH) / "hooks" / "rt_utf8.py"),
]

a = Analysis(
    [ENTRY],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=HOOKS,
    excludes=COMMON_EXCLUDES + [
        # backup doesn't need any of the heavy ML stack
        "torch", "transformers", "sentence_transformers",
        "fastapi", "starlette", "uvicorn",
        "psycopg", "pgvector",
        "fastmcp", "mcp",
        "googleapiclient",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="winserverrag-backup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="winserverrag-backup",
)
