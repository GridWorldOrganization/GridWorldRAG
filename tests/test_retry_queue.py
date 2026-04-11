"""再試行キュー helpers のテスト。

_load_failed_files / _save_failed_files のラウンドトリップと
_is_disk_full_error の判定を確認する。DB は使わない（MockConn で代替）。
"""

import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GRIDWORLDRAG_SKIP_CONFIG", "1")

import psycopg2.errors
import sync_rotate


class _FakeCursor:
    def __init__(self, store):
        self.store = store
        self._result = None

    def execute(self, sql, params=None):
        s = sql.strip().upper()
        if s.startswith("SELECT VALUE FROM SYNC_STATE"):
            key = params[0]
            self._result = [(self.store.get(key),)] if key in self.store else []
        elif s.startswith("INSERT INTO SYNC_STATE"):
            key, value = params[0], params[1]
            self.store[key] = value

    def fetchone(self):
        if self._result and len(self._result) > 0:
            return self._result[0]
        return None

    def close(self):
        pass


class _FakeConn:
    def __init__(self):
        self.store = {}

    def cursor(self):
        return _FakeCursor(self.store)

    def commit(self):
        pass


def test_failed_files_roundtrip_empty():
    conn = _FakeConn()
    assert sync_rotate._load_failed_files(conn) == []


def test_failed_files_save_and_load():
    conn = _FakeConn()
    queue = [
        {"drive_id": "ABC", "file_id": "1xyz"},
        {"drive_id": "DEF", "file_id": "2abc"},
    ]
    sync_rotate._save_failed_files(conn, queue)
    loaded = sync_rotate._load_failed_files(conn)
    assert loaded == queue


def test_failed_files_overwrite():
    conn = _FakeConn()
    sync_rotate._save_failed_files(conn, [{"file_id": "A"}])
    sync_rotate._save_failed_files(conn, [{"file_id": "B"}])
    loaded = sync_rotate._load_failed_files(conn)
    assert len(loaded) == 1
    assert loaded[0]["file_id"] == "B"


def test_failed_files_malformed_value_returns_empty():
    conn = _FakeConn()
    conn.store["failed_files"] = "not valid json"
    assert sync_rotate._load_failed_files(conn) == []


def test_is_disk_full_detects_psycopg2_diskfull():
    # psycopg2.errors.DiskFull は DatabaseError のサブクラス
    # 直接 raise はできないが isinstance 判定を検証
    class FakeDiskFull(psycopg2.errors.DiskFull):
        pass
    assert sync_rotate._is_disk_full_error(FakeDiskFull()) is True


def test_is_disk_full_detects_string_match():
    assert sync_rotate._is_disk_full_error(Exception("No space left on device")) is True
    assert sync_rotate._is_disk_full_error(Exception("disk full")) is True
    assert sync_rotate._is_disk_full_error(Exception("out of space"))  is True


def test_is_disk_full_ignores_unrelated_errors():
    assert sync_rotate._is_disk_full_error(Exception("connection refused")) is False
    assert sync_rotate._is_disk_full_error(Exception("auth failed")) is False
    assert sync_rotate._is_disk_full_error(ValueError("bad value")) is False


if __name__ == "__main__":
    test_failed_files_roundtrip_empty()
    test_failed_files_save_and_load()
    test_failed_files_overwrite()
    test_failed_files_malformed_value_returns_empty()
    test_is_disk_full_detects_psycopg2_diskfull()
    test_is_disk_full_detects_string_match()
    test_is_disk_full_ignores_unrelated_errors()
    print("All 7 tests passed.")
