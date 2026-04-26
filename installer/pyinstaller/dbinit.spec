# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — winserverrag-dbinit.exe

Wraps `python -m src.db_init`. Run once after install to bootstrap the
PostgreSQL database + global schema.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(SPECPATH).resolve().parent.parent))
from installer.pyinstaller._common import (  # noqa: E402
    project_root_from_specpath, COMMON_HIDDEN_IMPORTS, common_datas,
    COMMON_EXCLUDES,
)

PROJECT_ROOT = project_root_from_specpath(SPECPATH)
ENTRY = str(Path(PROJECT_ROOT) / "src" / "db_init.py")
HOOKS = [
    str(Path(SPECPATH) / "hooks" / "rt_utf8.py"),
]

# db_init only needs psycopg + the schema.sql data file. We still
# include COMMON_HIDDEN_IMPORTS to share the bundle layout — it costs
# nothing once the modules are pulled in by the API/daemon exes that
# share the same install dir.
a = Analysis(
    [ENTRY],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=common_datas(PROJECT_ROOT),
    hiddenimports=[
        "psycopg.types", "psycopg.rows", "pgvector.psycopg",
    ],
    hookspath=[],
    runtime_hooks=HOOKS,
    excludes=COMMON_EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="winserverrag-dbinit",
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
    name="winserverrag-dbinit",
)
