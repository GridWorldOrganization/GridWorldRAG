"""エラー耐性強化パッチの回帰防止テスト。

awesome-claude-code-toolkit の error-detective / chaos-engineer サブエージェントが
見つけた silent failure パターンに対する chaos-style fault injection テスト群。

対象:
1. db.file_exists の prefix 衝突 (ABCDE が ABCDEF_chunk_0 に誤ヒット)
2. LIKE パターンが literal underscore 境界を要求
3. _error_fallback が partial_content=True を立てる
4. extract_text のフォールバック is_partial フラグ
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GRIDWORLDRAG_SKIP_CONFIG", "1")

from src.db import _file_id_like_pattern


def test_pattern_requires_literal_underscore_separator():
    pattern = _file_id_like_pattern("ABCDE")
    assert pattern == r"ABCDE\_%"


def test_pattern_escapes_underscore_in_file_id():
    pattern = _file_id_like_pattern("AB_CD")
    assert pattern == r"AB\_CD\_%"


def test_pattern_escapes_percent_in_file_id():
    pattern = _file_id_like_pattern("AB%CD")
    assert pattern == r"AB\%CD\_%"


def test_pattern_escapes_backslash_in_file_id():
    pattern = _file_id_like_pattern("AB\\CD")
    assert pattern == r"AB\\CD\_%"


def test_pattern_for_real_drive_file_id():
    fid = "1vf7e333v_mmU5uzfFVdpNkVcruX1GwxWM8TP7MFM0qE"
    pattern = _file_id_like_pattern(fid)
    assert pattern == r"1vf7e333v\_mmU5uzfFVdpNkVcruX1GwxWM8TP7MFM0qE\_%"


def _pg_like_match(value, pattern, escape_char="\\"):
    """PostgreSQL LIKE の簡易エミュレーション (regex 経由)"""
    import re
    i = 0
    out = []
    while i < len(pattern):
        ch = pattern[i]
        if ch == escape_char and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if ch == "_":
            out.append(".")
        elif ch == "%":
            out.append(".*")
        else:
            out.append(re.escape(ch))
        i += 1
    regex = "^" + "".join(out) + "$"
    return re.match(regex, value) is not None


def test_old_pattern_has_prefix_collision():
    """旧バグの再現: "ABCDE%" は ABCDEF_chunk_0 に誤ヒット"""
    old_pattern = "ABCDE%"
    assert _pg_like_match("ABCDEF_chunk_0", old_pattern) is True


def test_new_pattern_avoids_prefix_collision():
    new_pattern = _file_id_like_pattern("ABCDE")
    assert _pg_like_match("ABCDEF_chunk_0", new_pattern) is False


def test_new_pattern_matches_correct_chunk():
    new_pattern = _file_id_like_pattern("ABCDE")
    assert _pg_like_match("ABCDE_chunk_0", new_pattern) is True


def test_new_pattern_matches_sheet_chunk():
    new_pattern = _file_id_like_pattern("ABCDE")
    assert _pg_like_match("ABCDE_sheet_123_chunk_0", new_pattern) is True


def test_new_pattern_does_not_match_unrelated():
    new_pattern = _file_id_like_pattern("ABCDE")
    assert _pg_like_match("XYZWV_chunk_0", new_pattern) is False
    assert _pg_like_match("ABCDEWV_chunk_0", new_pattern) is False


def test_new_pattern_with_underscore_in_file_id():
    new_pattern = _file_id_like_pattern("A_B")
    assert _pg_like_match("A_B_chunk_0", new_pattern) is True
    assert _pg_like_match("A1B_chunk_0", new_pattern) is False


def test_error_fallback_marks_partial_content():
    from src.indexer import make_chunk_entry
    import numpy as np

    file_info = {
        "id": "test_id",
        "name": "broken.docx",
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "modifiedTime": "2026-01-01T00:00:00Z",
        "owners": [],
    }
    emb = np.zeros(768, dtype=float)
    entry = make_chunk_entry(file_info, "[エラー] broken.docx", emb, 0, partial_content=True)
    assert entry["partial_content"] is True
    assert "[エラー]" in entry["content"]


def test_extract_text_unhandled_mime_marks_partial():
    from src import drive_client

    class _Svc:
        pass

    text, partial = drive_client.extract_text(_Svc(), {
        "id": "x", "name": "doc.docx",
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    })
    assert text == "[ファイル] doc.docx"
    assert partial is True


if __name__ == "__main__":
    test_pattern_requires_literal_underscore_separator()
    test_pattern_escapes_underscore_in_file_id()
    test_pattern_escapes_percent_in_file_id()
    test_pattern_escapes_backslash_in_file_id()
    test_pattern_for_real_drive_file_id()
    test_old_pattern_has_prefix_collision()
    test_new_pattern_avoids_prefix_collision()
    test_new_pattern_matches_correct_chunk()
    test_new_pattern_matches_sheet_chunk()
    test_new_pattern_does_not_match_unrelated()
    test_new_pattern_with_underscore_in_file_id()
    test_error_fallback_marks_partial_content()
    test_extract_text_unhandled_mime_marks_partial()
    print("All 13 tests passed.")
