from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest

from usage_dashboard.server.fetch_claude import fetch_claude_usage, refresh_claude_token
from usage_dashboard.server.fetch_ollama import fetch_ollama_usage
from usage_dashboard.server.fetch_types import FetchAuthError, FetchError
from usage_dashboard.server.fetch_umans import _format_tokens, fetch_umans_usage
from usage_dashboard.server.fetch_zai import fetch_zai_usage
from usage_dashboard.shared.models import Provider, ReadingStatus


def _claude_response_data():
    return {
        "five_hour": {
            "utilization_percent": 65.0,
            "reset_time": "2026-01-15T10:00:00Z",
        },
        "seven_day": {
            "utilization_percent": 45.0,
            "reset_time": "2026-01-19T00:00:00Z",
        },
    }


def _zai_response_data():
    # Mirrors the live response shape observed 2026-06-12: enveloped payload,
    # epoch-millisecond reset times, session = TOKENS_LIMIT unit 3.
    return {
        "code": 200,
        "msg": "Operation successful",
        "data": {
            "limits": [
                {
                    "type": "TOKENS_LIMIT",
                    "unit": 3,
                    "number": 5,
                    "percentage": 55,
                    "nextResetTime": 1781310923670,
                },
                {
                    "type": "TOKENS_LIMIT",
                    "unit": 6,
                    "number": 1,
                    "percentage": 35,
                    "nextResetTime": 1781663372979,
                },
                {
                    "type": "TIME_LIMIT",
                    "unit": 5,
                    "usage": 1000,
                    "currentValue": 37,
                    "percentage": 3,
                    "nextResetTime": 1783477772998,
                },
            ]
        },
    }


def _ollama_html(session_pct="72.5", weekly_pct="42.0"):
    return f"""
    <html><body>
    <span>Cloud Usage</span><span>Pro</span>
    <div><h3>Session usage</h3>
      <div><div style="width: {session_pct}%"></div></div>
      <span>{session_pct}% used</span>
      <span>Resets in <time data-time="2026-06-13T02:00:00Z">2h</time></span>
    </div>
    <div><h3>Weekly usage</h3>
      <div><div style="width: {weekly_pct}%"></div></div>
      <span>{weekly_pct}% used</span>
      <span>Resets in <time data-time="2026-06-17T00:00:00Z">4d</time></span>
    </div>
    </body></html>
    """


class TestFetchClaude:
    @patch("usage_dashboard.server.fetch_claude.httpx.Client")
    def test_fetch_claude_usage_returns_reading(self, mock_client_cls):
        mock_response = MagicMock()
        mock_response.json.return_value = _claude_response_data()
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        reading = fetch_claude_usage("test-token")
        assert reading.provider is Provider.CLAUDE
        assert reading.status is ReadingStatus.CURRENT
        assert reading.session_percent == 65.0
        assert reading.weekly_percent == 45.0
        assert reading.stale is False

    @patch("usage_dashboard.server.fetch_claude.httpx.Client")
    def test_fetch_claude_raises_fetch_error_on_http_failure(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.get.side_effect = httpx.HTTPError("fail")
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        try:
            fetch_claude_usage("test-token")
            assert False, "Should have raised FetchError"
        except FetchError:
            pass

    @patch("usage_dashboard.server.fetch_claude.httpx.Client")
    def test_fetch_claude_raises_fetch_error_on_malformed_response(self, mock_client_cls):
        mock_response = MagicMock()
        mock_response.json.return_value = {"unexpected": "data"}
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        try:
            fetch_claude_usage("test-token")
            assert False, "Should have raised FetchError"
        except FetchError:
            pass

    @patch("usage_dashboard.server.fetch_claude.httpx.Client")
    def test_fetch_claude_sends_authorization_header(self, mock_client_cls):
        mock_response = MagicMock()
        mock_response.json.return_value = _claude_response_data()
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        fetch_claude_usage("my-secret-token")
        call_args = mock_client.get.call_args
        headers = (
            call_args[1].get("headers", {})
            if call_args[1]
            else call_args[0][1] if len(call_args[0]) > 1 else {}
        )
        assert "Bearer my-secret-token" in str(headers) or any(
            "Bearer my-secret-token" in str(v) for v in headers.values()
        )


class TestFetchZai:
    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_fetch_zai_usage_returns_reading(self, mock_client_cls):
        mock_response = MagicMock()
        mock_response.json.return_value = _zai_response_data()
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        reading = fetch_zai_usage("test-key")
        assert reading.provider is Provider.ZAI
        assert reading.status is ReadingStatus.CURRENT
        assert reading.session_percent == 55.0
        assert reading.weekly_percent == 35.0
        assert reading.stale is False

    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_fetch_zai_raises_fetch_error_on_http_failure(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.get.side_effect = httpx.HTTPError("fail")
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        try:
            fetch_zai_usage("test-key")
            assert False, "Should have raised FetchError"
        except FetchError:
            pass

    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_fetch_zai_raises_fetch_error_on_malformed_response(self, mock_client_cls):
        mock_response = MagicMock()
        mock_response.json.return_value = {"wrong": "shape"}
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        try:
            fetch_zai_usage("test-key")
            assert False, "Should have raised FetchError"
        except FetchError:
            pass

    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_fetch_zai_missing_session_limit_raises(self, mock_client_cls):
        data = _zai_response_data()
        data["data"]["limits"] = [
            entry for entry in data["data"]["limits"] if entry.get("unit") != 3
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = data
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        try:
            fetch_zai_usage("test-key")
            assert False, "Should have raised FetchError"
        except FetchError:
            pass

    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_fetch_zai_missing_weekly_limit_raises(self, mock_client_cls):
        data = _zai_response_data()
        data["data"]["limits"] = [
            entry for entry in data["data"]["limits"] if entry.get("unit") != 6
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = data
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        try:
            fetch_zai_usage("test-key")
            assert False, "Should have raised FetchError"
        except FetchError:
            pass


class TestFetchOllama:
    @staticmethod
    def _mock_get(mock_client_cls, text="", status_code=200):
        response = MagicMock()
        response.text = text
        response.status_code = status_code
        response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client
        return mock_client

    @patch("usage_dashboard.server.fetch_ollama.httpx.Client")
    def test_fetch_ollama_usage_returns_reading(self, mock_client_cls):
        self._mock_get(mock_client_cls, text=_ollama_html())

        reading = fetch_ollama_usage("session=abc123")
        assert reading.provider is Provider.OLLAMA
        assert reading.status is ReadingStatus.CURRENT
        assert reading.session_percent == 72.5
        assert reading.weekly_percent == 42.0
        assert reading.session_resets_at == datetime(2026, 6, 13, 2, 0, 0)
        assert reading.weekly_resets_at == datetime(2026, 6, 17, 0, 0, 0)
        assert reading.stale is False

    @patch("usage_dashboard.server.fetch_ollama.httpx.Client")
    def test_fetch_ollama_sends_cookie_header(self, mock_client_cls):
        client = self._mock_get(mock_client_cls, text=_ollama_html())

        fetch_ollama_usage("session=abc123")
        headers = client.get.call_args[1]["headers"]
        assert headers["Cookie"] == "session=abc123"

    @patch("usage_dashboard.server.fetch_ollama.httpx.Client")
    def test_fetch_ollama_hourly_label_also_accepted(self, mock_client_cls):
        html = _ollama_html().replace("Session usage", "Hourly usage")
        self._mock_get(mock_client_cls, text=html)

        reading = fetch_ollama_usage("session=abc123")
        assert reading.session_percent == 72.5

    @patch("usage_dashboard.server.fetch_ollama.httpx.Client")
    def test_fetch_ollama_bar_width_fallback(self, mock_client_cls):
        html = _ollama_html().replace("% used", " percent")
        self._mock_get(mock_client_cls, text=html)

        reading = fetch_ollama_usage("session=abc123")
        assert reading.session_percent == 72.5

    @patch("usage_dashboard.server.fetch_ollama.httpx.Client")
    def test_fetch_ollama_raises_fetch_error_on_http_failure(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.get.side_effect = httpx.HTTPError("fail")
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        with pytest.raises(FetchError):
            fetch_ollama_usage("session=abc123")

    @patch("usage_dashboard.server.fetch_ollama.httpx.Client")
    def test_fetch_ollama_401_raises_auth_error(self, mock_client_cls):
        self._mock_get(mock_client_cls, status_code=401)

        with pytest.raises(FetchAuthError):
            fetch_ollama_usage("session=abc123")

    @patch("usage_dashboard.server.fetch_ollama.httpx.Client")
    def test_fetch_ollama_signed_out_page_raises_auth_error(self, mock_client_cls):
        html = '<html><body><h1>Sign in to Ollama</h1><form action="/signin"></form></body></html>'
        self._mock_get(mock_client_cls, text=html)

        with pytest.raises(FetchAuthError):
            fetch_ollama_usage("session=expired")

    @patch("usage_dashboard.server.fetch_ollama.httpx.Client")
    def test_fetch_ollama_missing_usage_raises_fetch_error(self, mock_client_cls):
        self._mock_get(mock_client_cls, text="<html><body>nothing here</body></html>")

        with pytest.raises(FetchError):
            fetch_ollama_usage("session=abc123")

    @patch("usage_dashboard.server.fetch_ollama.httpx.Client")
    def test_fetch_ollama_session_only_still_returns_reading(self, mock_client_cls):
        html = _ollama_html()
        html = html[: html.index("<h3>Weekly usage</h3>")] + "</body></html>"
        self._mock_get(mock_client_cls, text=html)

        reading = fetch_ollama_usage("session=abc123")
        assert reading.session_percent == 72.5
        assert reading.weekly_percent is None


class TestRefreshClaudeToken:
    @patch("usage_dashboard.server.fetch_claude.httpx.post")
    def test_refresh_returns_new_tokens(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        access, refresh = refresh_claude_token("old-refresh")
        assert access == "new-access"
        assert refresh == "new-refresh"

    @patch("usage_dashboard.server.fetch_claude.httpx.post")
    def test_refresh_uses_client_id_when_provided(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        refresh_claude_token("old-refresh", client_id="my-client-id")
        call_args = mock_post.call_args
        payload = call_args[1]["json"] if "json" in call_args[1] else call_args[0][1]
        assert payload["client_id"] == "my-client-id"

    @patch("usage_dashboard.server.fetch_claude.httpx.post")
    def test_refresh_reuses_old_refresh_when_not_returned(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "new-access",
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        access, refresh = refresh_claude_token("old-refresh")
        assert access == "new-access"
        assert refresh == "old-refresh"

    @patch("usage_dashboard.server.fetch_claude.httpx.post")
    def test_refresh_raises_fetch_error_on_http_failure(self, mock_post):
        mock_post.side_effect = httpx.HTTPError("fail")
        try:
            refresh_claude_token("old-refresh")
            assert False, "Should have raised FetchError"
        except FetchError:
            pass

    @patch("usage_dashboard.server.fetch_claude.httpx.post")
    def test_refresh_raises_fetch_error_on_malformed_response(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {"no_access_token": True}
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        try:
            refresh_claude_token("old-refresh")
            assert False, "Should have raised FetchError"
        except FetchError:
            pass


def _umans_response_data():
    return {
        "user_id": "00000000-0000-0000-0000-000000000000",
        "plan": {"slug": "code_max", "display_name": "Code Max"},
        "limits": {
            "requests": {"limit": None, "window_seconds": 18000},
            "concurrency": {"limit": 4, "description": "4 concurrent sessions"},
        },
        "window": {
            "started_at": "2026-06-12T19:29:48.490451+00:00",
            "resets_at": "2026-06-13T00:29:48.490451+00:00",
            "remaining_minutes": 98,
        },
        "usage": {
            "requests_in_window": 161,
            "concurrent_sessions": 2,
            "tokens_in": 63595096,
            "tokens_out": 320064,
            "tokens_cached": 61621120,
        },
    }


def _mock_umans_client(mock_client_cls, data):
    mock_response = MagicMock()
    mock_response.json.return_value = data
    mock_response.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client_cls.return_value.__enter__.return_value = mock_client


class TestFetchUmans:
    @patch("usage_dashboard.server.fetch_umans.httpx.Client")
    def test_fetch_umans_usage_returns_reading(self, mock_client_cls):
        _mock_umans_client(mock_client_cls, _umans_response_data())

        reading = fetch_umans_usage("test-key")

        assert reading.provider == Provider.UMANS
        assert reading.status == ReadingStatus.CURRENT
        assert reading.session_percent is None
        assert reading.weekly_percent is None
        assert reading.session_resets_at == datetime(2026, 6, 13, 0, 29, 48, 490451)
        assert reading.detail == "req 161  tok 63.9M"

    @patch("usage_dashboard.server.fetch_umans.httpx.Client")
    def test_missing_resets_at_yields_none(self, mock_client_cls):
        data = _umans_response_data()
        del data["window"]["resets_at"]
        _mock_umans_client(mock_client_cls, data)

        reading = fetch_umans_usage("test-key")

        assert reading.session_resets_at is None
        assert reading.detail == "req 161  tok 63.9M"

    @patch("usage_dashboard.server.fetch_umans.httpx.Client")
    def test_null_window_does_not_crash(self, mock_client_cls):
        data = _umans_response_data()
        data["window"] = None
        _mock_umans_client(mock_client_cls, data)

        reading = fetch_umans_usage("test-key")

        assert reading.session_resets_at is None
        assert reading.detail == "req 161  tok 63.9M"

    @patch("usage_dashboard.server.fetch_umans.httpx.Client")
    def test_http_error_raises_fetch_error(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.get.side_effect = httpx.ConnectError("boom")
        mock_client_cls.return_value.__enter__.return_value = mock_client

        with pytest.raises(FetchError):
            fetch_umans_usage("test-key")

    @patch("usage_dashboard.server.fetch_umans.httpx.Client")
    def test_malformed_response_raises_fetch_error(self, mock_client_cls):
        _mock_umans_client(mock_client_cls, {"usage": {}})

        with pytest.raises(FetchError):
            fetch_umans_usage("test-key")


class TestFormatTokens:
    def test_billions(self):
        assert _format_tokens(2_500_000_000) == "2.5B"

    def test_millions(self):
        assert _format_tokens(63_915_160) == "63.9M"

    def test_thousands(self):
        assert _format_tokens(4_200) == "4.2k"

    def test_small(self):
        assert _format_tokens(999) == "999"


class TestFetchErrorClassification:
    @staticmethod
    def _mock_status_client(mock_client_cls, status_code, headers=None):
        mock_response = MagicMock()
        mock_response.status_code = status_code
        mock_response.headers = headers or {}
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=MagicMock(), response=mock_response
        )
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

    @patch("usage_dashboard.server.fetch_claude.httpx.Client")
    def test_claude_401_raises_auth_error(self, mock_client_cls):
        self._mock_status_client(mock_client_cls, 401)
        with pytest.raises(FetchAuthError):
            fetch_claude_usage("token")

    @patch("usage_dashboard.server.fetch_claude.httpx.Client")
    def test_claude_403_raises_plain_fetch_error_not_auth(self, mock_client_cls):
        # A scope/permission 403 is permanent; it must NOT be a FetchAuthError,
        # so the scheduler never tries to refresh (and rotate) on it.
        self._mock_status_client(mock_client_cls, 403)
        with pytest.raises(FetchError) as excinfo:
            fetch_claude_usage("token")
        assert not isinstance(excinfo.value, FetchAuthError)
        assert "scope" in str(excinfo.value)

    @patch("usage_dashboard.server.fetch_claude.httpx.Client")
    def test_claude_429_raises_rate_limit_error_with_retry_after(self, mock_client_cls):
        from usage_dashboard.server.fetch_types import FetchRateLimitError

        self._mock_status_client(mock_client_cls, 429, headers={"retry-after": "608"})
        with pytest.raises(FetchRateLimitError) as excinfo:
            fetch_claude_usage("token")
        assert excinfo.value.retry_after_seconds == 608.0
        assert not isinstance(excinfo.value, FetchAuthError)

    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_zai_401_raises_auth_error(self, mock_client_cls):
        self._mock_status_client(mock_client_cls, 401)
        with pytest.raises(FetchAuthError):
            fetch_zai_usage("key")


class TestZaiResetTimes:
    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_epoch_millis_parsed_to_utc(self, mock_client_cls):
        mock_response = MagicMock()
        mock_response.json.return_value = _zai_response_data()
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        reading = fetch_zai_usage("key")

        assert reading.session_resets_at == datetime(2026, 6, 13, 0, 35, 23, 670000)
        assert reading.weekly_resets_at == datetime(2026, 6, 17, 2, 29, 32, 979000)
