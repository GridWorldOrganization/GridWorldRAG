"""_is_retriable_error の分類が正しいことを検証する。

db4 ビルド時に HTTP 500 (Internal Error) で GW_PJ 10,769 ファイルが silently 欠落する
バグが発生した。原因: _api_call_with_retry が "rate limit"/"429"/"quota" しかリトライ
対象としていなかった。5xx エラーは即 raise され、list_files_in_drive がページネーション
途中で失敗、ドライブ全体のフェッチが放棄されていた。

このテストは 5xx と rate limit 両方を網羅していることを保証する回帰防止。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GRIDWORLDRAG_SKIP_CONFIG", "1")

from src.drive_client import _is_retriable_error


# ---------------------------------------------------------------------------
# レート制限系 (既存動作の回帰防止)
# ---------------------------------------------------------------------------

def test_rate_limit_text_is_retriable():
    assert _is_retriable_error("Rate limit exceeded")


def test_429_is_retriable():
    assert _is_retriable_error("HttpError 429 Too Many Requests")


def test_quota_is_retriable():
    assert _is_retriable_error("Quota exceeded for user")


# ---------------------------------------------------------------------------
# HTTP 5xx (新規追加: db4 事故の根本原因対策)
# ---------------------------------------------------------------------------

def test_500_internal_error_is_retriable():
    # db4 事故で実際に発生したエラーメッセージ
    err = ('<HttpError 500 when requesting https://www.googleapis.com/drive/v3/files '
           'returned "Internal Error". Details: "[{\'message\': \'Internal Error\', '
           '\'domain\': \'global\', \'reason\': \'internalError\'}]">')
    assert _is_retriable_error(err), "HTTP 500 must be retriable (db4 regression)"


def test_502_bad_gateway_is_retriable():
    assert _is_retriable_error("HttpError 502 Bad Gateway")


def test_503_service_unavailable_is_retriable():
    assert _is_retriable_error("HttpError 503 Service Unavailable")


def test_504_gateway_timeout_is_retriable():
    assert _is_retriable_error("HttpError 504 Gateway Timeout")


def test_internal_error_text_is_retriable():
    # 番号なしでも文言で拾う
    assert _is_retriable_error('returned "Internal Error"')


def test_service_unavailable_text_is_retriable():
    assert _is_retriable_error("Service Unavailable")


# ---------------------------------------------------------------------------
# 接続レベルエラー (ネットワーク不安定)
# ---------------------------------------------------------------------------

def test_connection_reset_is_retriable():
    assert _is_retriable_error("Connection reset by peer")


def test_connection_aborted_is_retriable():
    assert _is_retriable_error("Connection aborted")


# ---------------------------------------------------------------------------
# リトライ対象ではないエラー (永続的失敗)
# ---------------------------------------------------------------------------

def test_404_not_found_is_not_retriable():
    # Drive からファイルが消えた場合。リトライしても同じ結果。
    assert not _is_retriable_error("HttpError 404 Not Found")


def test_403_forbidden_is_not_retriable():
    assert not _is_retriable_error("HttpError 403 Forbidden")


def test_400_bad_request_is_not_retriable():
    assert not _is_retriable_error("HttpError 400 Bad Request")


def test_auth_error_is_not_retriable():
    assert not _is_retriable_error("invalid_grant: Bad Request")


def test_key_error_is_not_retriable():
    assert not _is_retriable_error("KeyError: 'id'")


if __name__ == "__main__":
    test_rate_limit_text_is_retriable()
    test_429_is_retriable()
    test_quota_is_retriable()
    test_500_internal_error_is_retriable()
    test_502_bad_gateway_is_retriable()
    test_503_service_unavailable_is_retriable()
    test_504_gateway_timeout_is_retriable()
    test_internal_error_text_is_retriable()
    test_service_unavailable_text_is_retriable()
    test_connection_reset_is_retriable()
    test_connection_aborted_is_retriable()
    test_404_not_found_is_not_retriable()
    test_403_forbidden_is_not_retriable()
    test_400_bad_request_is_not_retriable()
    test_auth_error_is_not_retriable()
    test_key_error_is_not_retriable()
    print("All 16 tests passed.")
