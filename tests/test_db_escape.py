"""Unit tests for LIKE escape helpers (no DB needed)."""
from __future__ import annotations

from src.db import (
    _escape_like_literal,
    _file_id_like_pattern,
    schema_for_drive,
)


def test_escape_literal_handles_special_chars():
    # backslash must be first — otherwise other escaped chars' backslashes
    # would get re-escaped on the second pass.
    assert _escape_like_literal("a_b") == r"a\_b"
    assert _escape_like_literal("a%b") == r"a\%b"
    assert _escape_like_literal("a\\b") == r"a\\b"
    assert _escape_like_literal("plain") == "plain"


def test_file_id_pattern_forbids_prefix_collision():
    # File IDs "ABCDE" and "ABCDEF" must produce different patterns so
    # the longer one's chunks don't match the shorter's query.
    p_short = _file_id_like_pattern("ABCDE")
    p_long  = _file_id_like_pattern("ABCDEF")
    assert p_short == r"ABCDE\_%"
    assert p_long  == r"ABCDEF\_%"
    # The PATTERN ABCDE\_% does NOT match "ABCDEF_chunk_0" because the
    # escaped \_ is a literal underscore — and there is no underscore
    # between "ABCDE" and "F" in "ABCDEF_chunk_0".
    # (This is enforced at SQL level with ESCAPE '\'.)


def test_schema_for_drive_valid_ids():
    assert schema_for_drive("0AIp8raxJ7fQIUk9PVA") == "fd_0AIp8raxJ7fQIUk9PVA"


def test_schema_for_drive_converts_hyphen():
    # Hyphens must be replaced — PG identifiers don't allow '-' unquoted.
    assert schema_for_drive("0AEx49B8tZ-fqUk9PVA") == "fd_0AEx49B8tZ_fqUk9PVA"


def test_schema_for_drive_rejects_unsafe_input():
    import pytest
    with pytest.raises(ValueError):
        schema_for_drive("../../etc/passwd")
    with pytest.raises(ValueError):
        schema_for_drive("a;DROP TABLE x;--")
    with pytest.raises(ValueError):
        schema_for_drive("")
