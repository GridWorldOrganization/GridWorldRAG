# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — winserverrag-api.exe

Wraps `python -m src.control_api`. Includes schema.sql + web/ static UI
as bundled data so the running exe doesn't need a checked-out repo.
"""
import sys
from pathlib import Path

# SPECPATH is the dir containing this .spec at PyInstaller compile time.
sys.path.insert(0, str(Path(SPECPATH).resolve().parent.parent))
from installer.pyinstaller._common import (  # noqa: E402
    project_root_from_specpath, COMMON_HIDDEN_IMPORTS, common_datas,
    COMMON_EXCLUDES,
)

PROJECT_ROOT = project_root_from_specpath(SPECPATH)
ENTRY = str(Path(PROJECT_ROOT) / "src" / "control_api.py")
HOOKS = [
    str(Path(SPECPATH) / "hooks" / "rt_utf8.py"),
]

a = Analysis(
    [ENTRY],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=common_datas(PROJECT_ROOT),
    hiddenimports=COMMON_HIDDEN_IMPORTS,
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
    name="winserverrag-api",
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
    name="winserverrag-api",
)
