"""Shared analysis settings for the WinServerRAG PyInstaller specs.

The 4 entry points (api / daemon / db_init / db_backup) share the same
project layout, hidden imports, and runtime hooks. Centralising the
boilerplate here keeps the per-entry .spec files short and consistent.

Usage from a .spec:

    from installer.pyinstaller._common import (
        PROJECT_ROOT, COMMON_HIDDEN_IMPORTS, COMMON_DATAS,
        COMMON_EXCLUDES, RUNTIME_HOOKS,
    )
"""
from __future__ import annotations

import os
from pathlib import Path

# `__file__` is not defined when PyInstaller exec's a spec, but the
# specs all live next to this module, so resolving via SPECPATH works.
# Caller passes SPECPATH; we derive PROJECT_ROOT from there.
def project_root_from_specpath(specpath: str) -> str:
    return str(Path(specpath).resolve().parent.parent.parent)


# Hidden imports — modules PyInstaller's static analyser misses because
# they are imported via importlib / string-based factory lookups inside
# our deps. Verified on first build attempts on the master @ v1.1.0
# tree; expand here when new deps land.
COMMON_HIDDEN_IMPORTS = [
    # FastAPI / Starlette / Uvicorn dynamic loaders
    "uvicorn.lifespan.on",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.logging",
    # Pydantic v2 internals
    "pydantic.deprecated",
    "pydantic_core",
    # psycopg + pgvector
    "psycopg.types",
    "psycopg.rows",
    "pgvector.psycopg",
    # Sentence-transformers / torch dynamic registry
    "sentence_transformers.models",
    "sentence_transformers.util",
    "transformers",
    # FastMCP
    "fastmcp",
    "mcp.server.streamable_http",
    # Drive auth
    "google.auth.transport.requests",
    "google.oauth2.credentials",
    "googleapiclient.discovery",
    "googleapiclient.errors",
    "googleapiclient.http",
    # Misc
    "tiktoken_ext",
    "tiktoken_ext.openai_public",
]


# Data files — anything the runtime opens at import / startup that is NOT
# a Python module. PROJECT_ROOT is interpolated by the caller (specs
# pass it as `pr` since `Path` ops don't work inside a tuple comprehension
# in older PyInstaller versions).
def common_datas(project_root: str) -> list[tuple[str, str]]:
    return [
        # Global PG schema (apply_global_schema reads this at API startup).
        (os.path.join(project_root, "schema.sql"), "."),
        # Web static UI (FastAPI mounts web/ for the management page).
        (os.path.join(project_root, "web"), "web"),
    ]


# Excludes — packages that pull in heavy unused subtrees. tkinter is
# never imported by us; matplotlib is pulled in transitively by some
# torchvision setups but never used.
COMMON_EXCLUDES = [
    "tkinter",
    "matplotlib",
    "PyQt5", "PyQt6",
    "PySide2", "PySide6",
    "IPython", "ipykernel", "jupyter",
    "pytest",
    "_pytest",
]


# Runtime hooks — run inside the bundled exe before user code. We use
# them to set OMP_NUM_THREADS for the daemon (matches the .bat behavior)
# and to force UTF-8 on Windows console.
RUNTIME_HOOKS_DIR = "installer/pyinstaller/hooks"
