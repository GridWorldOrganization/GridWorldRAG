"""Runtime hook: force UTF-8 on Windows.

The daemon emits Japanese filenames in log lines. cp932 is the system
default on JP Windows and breaks on `[エラー]` style messages. Match
what the legacy .bat files set via PYTHONUTF8=1.
"""
import os
import sys

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# Reconfigure stdout/stderr for UTF-8 if attached to a console.
for stream_name in ("stdout", "stderr"):
    stream = getattr(sys, stream_name, None)
    if stream is not None:
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
