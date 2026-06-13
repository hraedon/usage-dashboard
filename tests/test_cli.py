from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from usage_dashboard.cli import (
    _CallbackHandler,
    _exchange_code,
    _generate_challenge,
    _generate_verifier,
)


class TestPKCE:
    def test_verifier_length(self) -> None:
        v = _generate_verifier()
        assert len(v) == 64

    def test_verifier_charset(self) -> None:
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")
        for _ in range(20):
            v = _generate_verifier()
            assert all(c in allowed for c in v)

    def test_challenge_is_base64url(self) -> None:
        v = _generate_verifier()
        challenge = _generate_challenge(v)
        # base64url: A-Z a-z 0-9 - _ (no padding)
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        assert all(c in allowed for c in challenge)

    def test_challenge_deterministic(self) -> None:
        v = "test_verifier_value_1234567890abcdefghijklmnop"
        c1 = _generate_challenge(v)
        c2 = _generate_challenge(v)
        assert c1 == c2

    def test_different_verifiers_different_challenges(self) -> None:
        c1 = _generate_challenge("verifier_a" * 5)
        c2 = _generate_challenge("verifier_b" * 5)
        assert c1 != c2


class TestExchangeCode:
    @patch("usage_dashboard.cli.httpx.post")
    def test_exchange_returns_tokens(self, mock_post: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        access, refresh = _exchange_code("auth-code", "verifier", "http://localhost/callback")
        assert access == "new-access"
        assert refresh == "new-refresh"

    @patch("usage_dashboard.cli.httpx.post")
    def test_exchange_sends_form_data(self, mock_post: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {"access_token": "a", "refresh_token": "r"}
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        _exchange_code("my-code", "my-verifier", "http://localhost:9999/callback")
        call_args = mock_post.call_args
        data = call_args[1].get("data") or call_args[0][1]
        assert data["grant_type"] == "authorization_code"
        assert data["code"] == "my-code"
        assert data["code_verifier"] == "my-verifier"
        assert data["redirect_uri"] == "http://localhost:9999/callback"

    @patch("usage_dashboard.cli.httpx.post")
    def test_exchange_raises_on_http_error(self, mock_post: MagicMock) -> None:
        mock_post.side_effect = httpx.HTTPError("fail")
        with pytest.raises(httpx.HTTPError):
            _exchange_code("code", "verifier", "http://localhost/callback")

    @patch("usage_dashboard.cli.httpx.post")
    def test_exchange_uses_fallback_refresh_token(self, mock_post: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {"access_token": "a"}
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        access, refresh = _exchange_code("code", "verifier", "http://localhost/callback")
        assert access == "a"
        assert refresh == ""


class TestCallbackHandler:
    def setup_method(self) -> None:
        _CallbackHandler.code = None
        _CallbackHandler.error = None

    def test_handler_captures_code(self) -> None:
        handler = _CallbackHandler.__new__(_CallbackHandler)
        handler.path = "/callback?code=test-code&state=abc"
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.wfile = MagicMock()
        handler.do_GET()
        assert _CallbackHandler.code == "test-code"

    def test_handler_captures_error(self) -> None:
        handler = _CallbackHandler.__new__(_CallbackHandler)
        handler.path = "/callback?error=access_denied"
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.wfile = MagicMock()
        handler.do_GET()
        assert _CallbackHandler.error == "access_denied"
