# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — winserverrag-daemon.exe

Wraps `python -m src.rag_daemon`. Caps OpenMP/MKL at 1 thread per
worker (matches `OMP_NUM_THREADS=1` in the legacy run_daemon.bat).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(SPECPATH).resolve().parent.parent))
from installer.pyinstaller._common import (  # noqa: E402
    project_root_from_specpath, COMMON_HIDDEN_IMPORTS, common_datas,
    COMMON_EXCLUDES,
)

PROJECT_ROOT = project_root_from_specpath(SPECPATH)
ENTRY = str(Path(PROJECT_ROOT) / "src" / "rag_daemon.py")
HOOKS = [
    str(Path(SPECPATH) / "hooks" / "rt_utf8.py"),
    str(Path(SPECPATH) / "hooks" / "rt_omp_threads.py"),
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
    name="winserverrag-daemon",
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
    name="winserverrag-daemon",
)
