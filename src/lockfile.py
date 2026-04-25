"""Single-instance lockfile (Windows-friendly, PID liveness probe)."""
from __future__ import annotations

import os
import time
from pathlib import Path

try:
    import psutil  # optional, better liveness check
    _HAS_PSUTIL = True
except Exception:  # pragma: no cover
    _HAS_PSUTIL = False


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if _HAS_PSUTIL:
        try:
            return psutil.pid_exists(pid)
        except Exception:
            return False
    # Fallback: Windows has no SIGCHLD; use OpenProcess via ctypes.
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if h == 0:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:
        return False


class LockAcquireError(RuntimeError):
    pass


class Lock:
    """mtime-bounded lockfile with PID liveness probe."""

    def __init__(self, path: Path, stale_after_sec: int = 1800):
        self.path = Path(path)
        self.stale_after_sec = stale_after_sec
        self._acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        my_pid = str(os.getpid())
        for _ in range(2):
            # Try atomic exclusive create first.
            try:
                with open(self.path, "x", encoding="utf-8") as f:
                    f.write(my_pid)
                self._acquired = True
                return
            except FileExistsError:
                pass
            # Inspect existing lock.
            try:
                pid = int(self.path.read_text(encoding="utf-8").strip())
            except Exception:
                pid = 0
            if _pid_alive(pid):
                age = time.time() - self.path.stat().st_mtime
                if age <= self.stale_after_sec:
                    raise LockAcquireError(f"lock held by pid {pid}: {self.path}")
            # Stale: remove and retry the atomic create.
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        raise LockAcquireError(f"could not acquire lock: {self.path}")

    def release(self) -> None:
        if self._acquired:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self._acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
