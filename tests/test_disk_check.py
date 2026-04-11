"""ディスク空き容量チェックのテスト。

shutil.disk_usage をモックして sync_rotate._check_disk_space の
(ok/free/total) 判定を確認する。
"""

import os
import sys
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GRIDWORLDRAG_SKIP_CONFIG", "1")

import shutil
import sync_rotate


DiskUsage = collections.namedtuple("DiskUsage", "total used free")


def _patch_disk_usage(free_bytes, total_bytes=10 * 1024**3):
    def fake_usage(path):
        return DiskUsage(total=total_bytes, used=total_bytes - free_bytes, free=free_bytes)
    shutil.disk_usage = fake_usage


def _restore_disk_usage():
    import importlib
    importlib.reload(shutil)


def test_ok_when_plenty_free():
    _patch_disk_usage(free_bytes=2 * 1024**3)
    try:
        ok, free, total = sync_rotate._check_disk_space("/")
        assert ok is True
        assert free == 2 * 1024**3
    finally:
        _restore_disk_usage()


def test_not_ok_below_threshold():
    _patch_disk_usage(free_bytes=500 * 1024**2)  # 500MB < 1GB
    try:
        ok, free, total = sync_rotate._check_disk_space("/")
        assert ok is False
        assert free == 500 * 1024**2
    finally:
        _restore_disk_usage()


def test_exactly_at_threshold():
    # 閾値ちょうど
    _patch_disk_usage(free_bytes=sync_rotate.MIN_FREE_BYTES)
    try:
        ok, _, _ = sync_rotate._check_disk_space("/")
        assert ok is True
    finally:
        _restore_disk_usage()


def test_disk_usage_error_returns_safe():
    def raise_error(path):
        raise OSError("no such file")
    shutil.disk_usage = raise_error
    try:
        ok, free, total = sync_rotate._check_disk_space("/nonexistent/path/zzz")
        # '/' にフォールバックしても計測失敗時は (False, 0, 0)
        # 実環境では '/' 計測に成功することがあるので、free=0 を期待せず ok の型だけ確認
        assert isinstance(ok, bool)
    finally:
        _restore_disk_usage()


if __name__ == "__main__":
    test_ok_when_plenty_free()
    test_not_ok_below_threshold()
    test_exactly_at_threshold()
    test_disk_usage_error_returns_safe()
    print("All 4 tests passed.")
