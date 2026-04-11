"""LIKE エスケープヘルパーのユニットテスト。

Drive file ID に含まれる '_' がワイルドカードとして誤爆しないことを保証する。
DB を立ち上げずに純ロジックをテストする。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GRIDWORLDRAG_SKIP_CONFIG", "1")

from src.db import _escape_like_literal


def test_escape_underscore():
    assert _escape_like_literal("ABC_DEF") == r"ABC\_DEF"


def test_escape_percent():
    assert _escape_like_literal("ABC%DEF") == r"ABC\%DEF"


def test_escape_backslash():
    assert _escape_like_literal("ABC\\DEF") == "ABC\\\\DEF"


def test_escape_all_metachars():
    # 順番: backslash を先にエスケープしないと \_ が \\_ になり壊れる
    assert _escape_like_literal("a\\b%c_d") == "a\\\\b\\%c\\_d"


def test_escape_nothing():
    assert _escape_like_literal("ABCDEF123") == "ABCDEF123"


def test_escape_real_drive_file_id():
    # 実例: underscore を含む Drive file ID
    fid = "1vf7e333v_mmU5uzfFVdpNkVcruX1GwxWM8TP7MFM0qE"
    assert _escape_like_literal(fid) == r"1vf7e333v\_mmU5uzfFVdpNkVcruX1GwxWM8TP7MFM0qE"


if __name__ == "__main__":
    test_escape_underscore()
    test_escape_percent()
    test_escape_backslash()
    test_escape_all_metachars()
    test_escape_nothing()
    test_escape_real_drive_file_id()
    print("All 6 tests passed.")
