"""sync_rotate.py の lockfile 処理の挙動テスト。

外部依存なしで lockfile 3 分岐（未作成 / fresh / stale）を検証する。
"""

import os
import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GRIDWORLDRAG_SKIP_CONFIG", "1")

import sync_rotate


def _with_temp_lockfile(test_func):
    """各テストで一時ディレクトリに lockfile を切り替える。"""
    def wrapper():
        tmpdir = tempfile.mkdtemp(prefix="test_lock_")
        original = sync_rotate.LOCK_FILE
        sync_rotate.LOCK_FILE = Path(tmpdir) / "test.lock"
        try:
            test_func()
        finally:
            if sync_rotate.LOCK_FILE.exists():
                sync_rotate.LOCK_FILE.unlink()
            os.rmdir(tmpdir)
            sync_rotate.LOCK_FILE = original
    return wrapper


@_with_temp_lockfile
def test_acquire_when_no_lock():
    assert not sync_rotate.LOCK_FILE.exists()
    sync_rotate._acquire_lock()
    assert sync_rotate.LOCK_FILE.exists()
    pid = sync_rotate.LOCK_FILE.read_text()
    assert pid == str(os.getpid())


@_with_temp_lockfile
def test_release_removes_lock():
    sync_rotate._acquire_lock()
    assert sync_rotate.LOCK_FILE.exists()
    sync_rotate._release_lock()
    assert not sync_rotate.LOCK_FILE.exists()


@_with_temp_lockfile
def test_release_is_idempotent():
    # 存在しなくてもエラーにならない
    sync_rotate._release_lock()
    sync_rotate._release_lock()


@_with_temp_lockfile
def test_acquire_exits_on_live_pid():
    """現在実行中のプロセス PID が書かれていたら exit(0)。"""
    sync_rotate.LOCK_FILE.write_text(str(os.getpid()))
    try:
        sync_rotate._acquire_lock()
    except SystemExit as e:
        assert e.code == 0
        return
    assert False, "should have exited on live PID"


@_with_temp_lockfile
def test_acquire_takes_over_dead_pid():
    """死んだ PID が書かれていたら mtime に関わらず takeover。"""
    # PID 99999 は普通存在しない（kernel PID_MAX を超える環境は稀）
    # 念のため使用中でないことを確認してから書く
    dead_pid = 99999
    try:
        os.kill(dead_pid, 0)
        dead_pid = 99998  # 別の候補
    except (ProcessLookupError, PermissionError):
        pass
    sync_rotate.LOCK_FILE.write_text(str(dead_pid))
    # わざと fresh mtime (今) のまま
    sync_rotate._acquire_lock()
    assert sync_rotate.LOCK_FILE.read_text() == str(os.getpid())


@_with_temp_lockfile
def test_acquire_takes_over_stale_lock_unreadable_pid():
    """PID が読めない破損 lockfile は mtime fallback で stale 扱い。"""
    sync_rotate.LOCK_FILE.write_text("garbage-not-an-int")
    old_time = time.time() - 1800  # 30分前 (>_STALE_LOCK_SEC=1200)
    os.utime(sync_rotate.LOCK_FILE, (old_time, old_time))

    sync_rotate._acquire_lock()
    assert sync_rotate.LOCK_FILE.read_text() == str(os.getpid())


@_with_temp_lockfile
def test_acquire_fresh_unreadable_pid_exits():
    """PID 読めない + 新しい mtime → fallback で exit(0)。"""
    sync_rotate.LOCK_FILE.write_text("")  # 空ファイル
    try:
        sync_rotate._acquire_lock()
    except SystemExit as e:
        assert e.code == 0
        return
    assert False, "should have exited on fresh unreadable lock"


if __name__ == "__main__":
    test_acquire_when_no_lock()
    test_release_removes_lock()
    test_release_is_idempotent()
    test_acquire_exits_on_live_pid()
    test_acquire_takes_over_dead_pid()
    test_acquire_takes_over_stale_lock_unreadable_pid()
    test_acquire_fresh_unreadable_pid_exits()
    print("All 7 tests passed.")
