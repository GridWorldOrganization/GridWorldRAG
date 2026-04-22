"""Unit tests for src.mcp_auth (no DB needed)."""
from __future__ import annotations

from src.mcp_auth import hash_password, verify_password


def test_roundtrip_accepts_correct_password():
    h = hash_password("admin")
    assert verify_password("admin", h) is True


def test_rejects_wrong_password():
    h = hash_password("secret123")
    assert verify_password("wrongpw", h) is False


def test_rejects_empty_and_garbage():
    h = hash_password("x")
    assert verify_password("", h) is False
    assert verify_password("admin", "garbage") is False
    assert verify_password("admin", "pbkdf2_sha256$notanumber$salt$hash") is False
    assert verify_password("admin", "md5$1$s$h") is False


def test_hashes_are_unique_per_call():
    # Same password, different salts -> different stored strings.
    assert hash_password("samepw") != hash_password("samepw")
