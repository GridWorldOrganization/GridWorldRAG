"""Unit tests for src.lockfile (no DB, no network)."""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

from src.lockfile import Lock, LockAcquireError


def _tmp_lock_path() -> Path:
    d = Path(tempfile.mkdtemp(prefix="winsvr_test_"))
    return d / "test.lock"


def test_acquires_when_no_existing_lock():
    p = _tmp_lock_path()
    lock = Lock(p, stale_after_sec=60)
    lock.acquire()
    try:
        assert p.exists()
        assert int(p.read_text(encoding="utf-8").strip()) == os.getpid()
    finally:
        lock.release()
        assert not p.exists()


def test_rejects_when_live_pid_holds_lock():
    p = _tmp_lock_path()
    # Write the current process's PID (which is alive).
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(os.getpid()), encoding="utf-8")
    lock = Lock(p, stale_after_sec=3600)
    with pytest.raises(LockAcquireError):
        lock.acquire()
    p.unlink()


def test_breaks_stale_lock_from_dead_pid():
    p = _tmp_lock_path()
    # Pick a PID that should not exist. On Windows typical user-mode PIDs
    # are in thousands; 999999 is almost certainly unused.
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("999999", encoding="utf-8")
    lock = Lock(p, stale_after_sec=3600)
    lock.acquire()
    try:
        assert int(p.read_text(encoding="utf-8").strip()) == os.getpid()
    finally:
        lock.release()


def test_breaks_stale_lock_from_old_mtime():
    """Even with a 'live' PID in the lock file, if the mtime is older than
    stale_after_sec, the lock is considered abandoned. This covers the case
    of a process stuck in an infinite loop (alive but not progressing)."""
    p = _tmp_lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(os.getpid()), encoding="utf-8")  # live PID
    old = time.time() - 1800
    os.utime(p, (old, old))
    lock = Lock(p, stale_after_sec=60)  # 60s threshold, age 30min -> stale
    lock.acquire()
    try:
        assert int(p.read_text(encoding="utf-8").strip()) == os.getpid()
    finally:
        lock.release()
