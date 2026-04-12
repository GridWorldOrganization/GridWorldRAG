"""authenticate() の OAuth token refresh が一時エラーをリトライすることを検証する。

issue #11: creds.refresh(Request()) は oauth2.googleapis.com に依存しており、
5xx や connection reset で瞬断することがある。リトライなしだと launchd 次回
実行(5分後)までの間同期が停止するため、_api_call_with_retry で吸収する。
"""

import os
import sys
import pickle
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GRIDWORLDRAG_SKIP_CONFIG", "1")
os.environ.setdefault("GOOGLE_EMAIL", "test@example.com")
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")

import src.drive_client as drive_client


class _FakeCreds:
    """google.oauth2.credentials.Credentials の最小モック。"""

    def __init__(self, refresh_side_effects):
        self.valid = False
        self.expired = True
        self.refresh_token = "fake-refresh-token"
        self._side_effects = list(refresh_side_effects)
        self.refresh_calls = 0

    def refresh(self, request):
        self.refresh_calls += 1
        if self._side_effects:
            eff = self._side_effects.pop(0)
            if isinstance(eff, Exception):
                raise eff
        # 最終的に成功した扱いにする
        self.valid = True
        self.expired = False


def _run_authenticate(creds):
    """テスト用に authenticate() を最小依存で実行する。

    httplib2 / google_auth_httplib2 は authenticate() 内で import されるので
    sys.modules に事前に stub を差し込む。
    """
    fake_httplib2 = mock.MagicMock()
    fake_httplib2.Http.return_value = "fake-http2"
    fake_gah = mock.MagicMock()
    fake_gah.AuthorizedHttp.return_value = "fake-http"
    module_stubs = {
        "httplib2": fake_httplib2,
        "google_auth_httplib2": fake_gah,
    }
    with mock.patch.dict(sys.modules, module_stubs), \
         mock.patch("src.drive_client.TOKEN_PATH") as token_path, \
         mock.patch("src.drive_client.pickle") as fake_pickle, \
         mock.patch("src.drive_client.build") as fake_build, \
         mock.patch("builtins.open", mock.mock_open()), \
         mock.patch("src.drive_client._time.sleep"):
        token_path.exists.return_value = True
        fake_pickle.load.return_value = creds
        fake_pickle.dump.return_value = None
        fake_build.return_value = "fake-service"
        drive_client.authenticate()


def test_refresh_success_on_first_try():
    creds = _FakeCreds([])
    _run_authenticate(creds)
    assert creds.refresh_calls == 1


def test_refresh_retries_on_500():
    class HttpError500(Exception):
        def __str__(self):
            return "HttpError 500 Internal Error from oauth2.googleapis.com"
    creds = _FakeCreds([HttpError500()])
    _run_authenticate(creds)
    # 1 回失敗 → 1 回成功 = 2 回呼ばれる
    assert creds.refresh_calls == 2


def test_refresh_retries_on_connection_reset():
    class ConnReset(Exception):
        def __str__(self):
            return "Connection reset by peer"
    creds = _FakeCreds([ConnReset(), ConnReset()])
    _run_authenticate(creds)
    assert creds.refresh_calls == 3


def test_refresh_raises_on_non_retriable_error():
    class AuthError(Exception):
        def __str__(self):
            return "invalid_grant: Token has been expired or revoked."
    creds = _FakeCreds([AuthError()])
    try:
        _run_authenticate(creds)
    except AuthError:
        assert creds.refresh_calls == 1
        return
    assert False, "should have raised AuthError (non-retriable)"


if __name__ == "__main__":
    test_refresh_success_on_first_try()
    test_refresh_retries_on_500()
    test_refresh_retries_on_connection_reset()
    test_refresh_raises_on_non_retriable_error()
    print("All 4 tests passed.")
