"""build_parallel.py の /tmp 空き容量プリフライトをテストする。

issue #12: taskdata.pkl / filelist.pkl の書き込みが ENOSPC で失敗すると
Queue に空タスクが流れ silent data loss を起こす。起動時に空き容量をチェック
して不足時は即 exit(2) する。
"""

import os
import sys
from collections import namedtuple
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GRIDWORLDRAG_SKIP_CONFIG", "1")
os.environ.setdefault("GOOGLE_EMAIL", "test@example.com")
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")

import build_parallel

_DiskUsage = namedtuple("_DiskUsage", ["total", "used", "free"])


def test_preflight_passes_when_plenty_of_space():
    """空き容量十分 → 正常リターン。"""
    fake_usage = _DiskUsage(total=100 * 1024**3, used=10 * 1024**3, free=90 * 1024**3)
    with mock.patch("shutil.disk_usage", return_value=fake_usage):
        build_parallel._preflight_tmp_disk_space(min_free_bytes=500 * 1024 * 1024)


def test_preflight_exits_on_low_space():
    """空き容量不足 → exit(2)。"""
    fake_usage = _DiskUsage(total=100 * 1024**3, used=99 * 1024**3, free=100 * 1024 * 1024)  # 100MB free
    with mock.patch("shutil.disk_usage", return_value=fake_usage):
        try:
            build_parallel._preflight_tmp_disk_space(min_free_bytes=500 * 1024 * 1024)
        except SystemExit as e:
            assert e.code == 2
            return
    assert False, "should have exited with code 2"


def test_preflight_tolerates_os_error():
    """disk_usage が例外時は警告のみで継続 (preflight で build を止めたくない場合)。"""
    with mock.patch("shutil.disk_usage", side_effect=OSError("mock permission denied")):
        # exit しないこと
        build_parallel._preflight_tmp_disk_space(min_free_bytes=500 * 1024 * 1024)


def test_preflight_boundary_exact_threshold_ok():
    """free == threshold は OK (< で判定)。"""
    fake_usage = _DiskUsage(total=100 * 1024**3, used=0, free=500 * 1024 * 1024)
    with mock.patch("shutil.disk_usage", return_value=fake_usage):
        build_parallel._preflight_tmp_disk_space(min_free_bytes=500 * 1024 * 1024)


def test_preflight_boundary_under_threshold_exits():
    """free < threshold は exit。"""
    fake_usage = _DiskUsage(total=100 * 1024**3, used=0, free=500 * 1024 * 1024 - 1)
    with mock.patch("shutil.disk_usage", return_value=fake_usage):
        try:
            build_parallel._preflight_tmp_disk_space(min_free_bytes=500 * 1024 * 1024)
        except SystemExit as e:
            assert e.code == 2
            return
    assert False, "should have exited"


if __name__ == "__main__":
    test_preflight_passes_when_plenty_of_space()
    test_preflight_exits_on_low_space()
    test_preflight_tolerates_os_error()
    test_preflight_boundary_exact_threshold_ok()
    test_preflight_boundary_under_threshold_exits()
    print("All 5 tests passed.")
