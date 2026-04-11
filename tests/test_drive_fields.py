"""drive_client.py の Changes API fields 文字列の妥当性検証。

Drive API は fields 文字列の typo でリクエスト全体を 400 で拒否するため、
構文（カッコ、トップレベルキー、ネストキー）をホワイトリストで検証する。
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GRIDWORLDRAG_SKIP_CONFIG", "1")

from src.drive_client import _CHANGES_FIELDS


_TOP_LEVEL_ALLOWED = {"nextPageToken", "newStartPageToken", "changes"}
_CHANGE_ALLOWED = {"fileId", "removed", "file"}
_FILE_ALLOWED = {
    "id", "name", "mimeType", "modifiedTime", "trashed",
    "owners", "webViewLink", "driveId", "parents", "permissions",
}
_PERMISSION_ALLOWED = {"emailAddress", "role", "type", "displayName"}


def _balanced_parens(s):
    depth = 0
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _extract_tokens(s):
    """ネストを無視してトップレベルのカンマ区切りトークンを返す。"""
    tokens = []
    buf = ""
    depth = 0
    for ch in s:
        if ch == "(":
            depth += 1
            buf += ch
        elif ch == ")":
            depth -= 1
            buf += ch
        elif ch == "," and depth == 0:
            tokens.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        tokens.append(buf.strip())
    return tokens


def _key_of(token):
    """'key(...)' 形式からキー名のみ抽出。"""
    m = re.match(r"^([a-zA-Z]+)", token)
    return m.group(1) if m else None


def _inner_of(token):
    m = re.match(r"^[a-zA-Z]+\((.*)\)$", token)
    return m.group(1) if m else None


def test_balanced_parens():
    assert _balanced_parens(_CHANGES_FIELDS), "fields string has unbalanced parens"


def test_top_level_keys():
    tokens = _extract_tokens(_CHANGES_FIELDS)
    keys = {_key_of(t) for t in tokens}
    unknown = keys - _TOP_LEVEL_ALLOWED
    assert not unknown, f"unknown top-level keys: {unknown}"
    assert "changes" in keys, "missing 'changes' key"


def test_changes_nested_keys():
    # changes(...) の中身を取り出して検証
    tokens = _extract_tokens(_CHANGES_FIELDS)
    changes_token = next(t for t in tokens if _key_of(t) == "changes")
    inner = _inner_of(changes_token)
    assert inner is not None, "changes key has no inner group"
    nested = _extract_tokens(inner)
    keys = {_key_of(t) for t in nested}
    unknown = keys - _CHANGE_ALLOWED
    assert not unknown, f"unknown changes.* keys: {unknown}"


def test_file_nested_keys():
    tokens = _extract_tokens(_CHANGES_FIELDS)
    changes_token = next(t for t in tokens if _key_of(t) == "changes")
    inner = _inner_of(changes_token)
    nested = _extract_tokens(inner)
    file_token = next(t for t in nested if _key_of(t) == "file")
    file_inner = _inner_of(file_token)
    file_keys = _extract_tokens(file_inner)
    keys = {_key_of(t) for t in file_keys}
    unknown = keys - _FILE_ALLOWED
    assert not unknown, f"unknown file.* keys: {unknown}"


def test_permissions_nested_keys():
    tokens = _extract_tokens(_CHANGES_FIELDS)
    changes_token = next(t for t in tokens if _key_of(t) == "changes")
    nested = _extract_tokens(_inner_of(changes_token))
    file_token = next(t for t in nested if _key_of(t) == "file")
    file_keys = _extract_tokens(_inner_of(file_token))
    perm_token = next(t for t in file_keys if _key_of(t) == "permissions")
    perm_inner = _inner_of(perm_token)
    perm_keys = {_key_of(t) for t in _extract_tokens(perm_inner)}
    unknown = perm_keys - _PERMISSION_ALLOWED
    assert not unknown, f"unknown permissions.* keys: {unknown}"


if __name__ == "__main__":
    test_balanced_parens()
    test_top_level_keys()
    test_changes_nested_keys()
    test_file_nested_keys()
    test_permissions_nested_keys()
    print("All 5 tests passed.")
