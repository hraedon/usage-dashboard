from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from usage_dashboard.cli import (
    _CLAUDE_CLIENT_ID,
    _CLAUDE_SCOPES,
    _CallbackHandler,
    _exchange_code,
    _generate_challenge,
    _generate_verifier,
    _ollama_cookies,
    _opencode_auth_cookie,
    _parse_pasted_input,
    _serialize_cookie_header,
    _workspace_id_from,
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


class TestOAuthConstants:
    def test_client_id_is_a_uuid_not_a_url(self) -> None:
        # Regression: an earlier draft set this to a metadata URL, which the
        # token endpoint rejects. Claude Code's public client is a UUID.
        assert not _CLAUDE_CLIENT_ID.startswith("http")
        assert _CLAUDE_CLIENT_ID.count("-") == 4

    def test_scopes_include_user_profile(self) -> None:
        # The usage endpoint returns 403 without user:profile.
        assert "user:profile" in _CLAUDE_SCOPES.split()

    def test_scopes_match_current_claude_code(self) -> None:
        # Mirror the scope set the current Claude Code CLI requests, so the
        # authorize server issues a code it will accept at exchange time.
        assert _CLAUDE_SCOPES.split() == [
            "user:inference",
            "user:profile",
            "user:sessions:claude_code",
            "user:mcp_servers",
        ]


class TestExchangeIncludesClientAndState:
    @patch("usage_dashboard.cli.httpx.post")
    def test_exchange_sends_client_id(self, mock_post: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {"access_token": "a", "refresh_token": "r"}
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        _exchange_code("code", "verifier", "http://localhost:9/callback", state="s1")
        data = mock_post.call_args[1].get("data") or mock_post.call_args[0][1]
        assert data["client_id"] == _CLAUDE_CLIENT_ID
        assert data["state"] == "s1"

    @patch("usage_dashboard.cli.httpx.post")
    def test_exchange_omits_state_when_none(self, mock_post: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {"access_token": "a", "refresh_token": "r"}
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        _exchange_code("code", "verifier", "http://localhost:9/callback")
        data = mock_post.call_args[1].get("data") or mock_post.call_args[0][1]
        assert "state" not in data


class TestExchangeErrors:
    @patch("usage_dashboard.cli.httpx.post")
    def test_exchange_surfaces_oauth_error(self, mock_post: MagicMock) -> None:
        # A rejected exchange must surface the server's error rather than
        # crashing on a bare KeyError for the missing access_token.
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.json.return_value = {
            "error": "invalid_grant",
            "error_description": "Invalid 'code' in request.",
        }
        mock_post.return_value = mock_response

        with pytest.raises(httpx.HTTPError) as exc_info:
            _exchange_code("code", "verifier", "http://localhost/callback")
        assert "invalid_grant" in str(exc_info.value)
        assert "Invalid 'code' in request." in str(exc_info.value)

    @patch("usage_dashboard.cli.httpx.post")
    def test_exchange_surfaces_oauth_error_dict(self, mock_post: MagicMock) -> None:
        # Anthropic sometimes returns error as a nested object rather than a
        # plain string; still surface it cleanly.
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.json.return_value = {
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "Invalid JSON body"},
        }
        mock_post.return_value = mock_response

        with pytest.raises(httpx.HTTPError) as exc_info:
            _exchange_code("code", "verifier", "http://localhost/callback")
        assert "invalid_request_error" in str(exc_info.value)
        assert "Invalid JSON body" in str(exc_info.value)

    @patch("usage_dashboard.cli.httpx.post")
    def test_exchange_surfaces_non_json_body(self, mock_post: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 502
        mock_response.json.side_effect = ValueError("not json")
        mock_post.return_value = mock_response

        with pytest.raises(httpx.HTTPError) as exc_info:
            _exchange_code("code", "verifier", "http://localhost/callback")
        assert "non-JSON" in str(exc_info.value)

    @patch("usage_dashboard.cli.httpx.post")
    def test_exchange_surfaces_missing_access_token(self, mock_post: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"refresh_token": "r"}
        mock_post.return_value = mock_response

        with pytest.raises(httpx.HTTPError) as exc_info:
            _exchange_code("code", "verifier", "http://localhost/callback")
        assert "missing access_token" in str(exc_info.value)


class TestAuthorizeUrl:
    @patch("usage_dashboard.cli._wait_for_code")
    @patch("usage_dashboard.cli.httpx.post")
    @patch("usage_dashboard.cli.secrets.token_urlsafe", return_value="fixed-state")
    def test_authorize_url_includes_code_true(
        self,
        mock_token: MagicMock,
        mock_post: MagicMock,
        mock_wait: MagicMock,
        capsys,
    ) -> None:
        # Claude's authorize endpoint only issues a usable PKCE code when
        # code=true is present (the real CLI always sends it first). Regression
        # for WI-013: the login path omitted it and the exchange then failed.
        mock_wait.return_value = ("auth-code", "fixed-state", None)
        mock_response = MagicMock()
        mock_response.json.return_value = {"access_token": "a", "refresh_token": "r"}
        mock_post.return_value = mock_response

        from usage_dashboard.cli import login_claude

        login_claude(port=8765, no_browser=True)

        out = capsys.readouterr().out
        assert "code=true" in out
        data = mock_post.call_args[1]["data"]
        assert data["redirect_uri"] == "http://localhost:8765/callback"
        assert data["code"] == "auth-code"


class TestParsePastedInput:
    def test_bare_code(self) -> None:
        assert _parse_pasted_input("abc123") == ("abc123", None)

    def test_code_hash_state(self) -> None:
        assert _parse_pasted_input("abc123#xyz") == ("abc123", "xyz")

    def test_full_redirect_url(self) -> None:
        code, state = _parse_pasted_input(
            "https://platform.claude.com/oauth/code/callback?code=abc&state=xyz"
        )
        assert code == "abc"
        assert state == "xyz"

    def test_whitespace_trimmed(self) -> None:
        assert _parse_pasted_input("  abc123#xyz  ") == ("abc123", "xyz")

    def test_empty(self) -> None:
        assert _parse_pasted_input("") == (None, None)


class TestCallbackHandler:
    def setup_method(self) -> None:
        _CallbackHandler.code = None
        _CallbackHandler.state = None
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


class TestOllamaCookieHelpers:
    def test_filters_to_ollama_domain(self) -> None:
        cookies = [
            {"name": "session", "value": "abc", "domain": "ollama.com"},
            {"name": "sub", "value": "x", "domain": ".ollama.com"},
            {"name": "other", "value": "y", "domain": "workos.com"},
            {"name": "ga", "value": "z", "domain": ".google.com"},
        ]
        kept = _ollama_cookies(cookies)
        names = {c["name"] for c in kept}
        assert names == {"session", "sub"}

    def test_serialize_cookie_header(self) -> None:
        cookies = [
            {"name": "session", "value": "abc", "domain": "ollama.com"},
            {"name": "sub", "value": "x", "domain": ".ollama.com"},
        ]
        assert _serialize_cookie_header(cookies) == "session=abc; sub=x"

    def test_empty_when_no_ollama_cookies(self) -> None:
        cookies = [{"name": "other", "value": "y", "domain": "workos.com"}]
        assert _serialize_cookie_header(_ollama_cookies(cookies)) == ""


class TestOpenCodeLoginHelpers:
    def test_picks_the_auth_cookie_for_opencode(self) -> None:
        cookies = [
            {"name": "auth", "value": "Fe26.2**deadbeef", "domain": "opencode.ai"},
            {"name": "auth", "value": "someone-else", "domain": "example.com"},
            {"name": "ga", "value": "z", "domain": ".google.com"},
        ]
        assert _opencode_auth_cookie(cookies) == "Fe26.2**deadbeef"

    def test_none_when_no_auth_cookie(self) -> None:
        cookies = [{"name": "session", "value": "x", "domain": "opencode.ai"}]
        assert _opencode_auth_cookie(cookies) is None

    def test_ignores_an_auth_cookie_from_another_domain(self) -> None:
        # A same-named cookie from an unrelated site must not be captured as
        # the opencode credential.
        cookies = [{"name": "auth", "value": "wrong", "domain": "notopencode.com"}]
        assert _opencode_auth_cookie(cookies) is None

    def test_workspace_id_from_url(self) -> None:
        url = "https://opencode.ai/workspace/wrk_01JTESTWRKSPACE00000000000/go"
        assert _workspace_id_from(url) == "wrk_01JTESTWRKSPACE00000000000"

    def test_workspace_id_falls_back_to_page_html(self) -> None:
        html = '<a href="/workspace/wrk_01JTESTWRKSPACE00000000000/go">Go</a>'
        got = _workspace_id_from("https://opencode.ai/", html)
        assert got == "wrk_01JTESTWRKSPACE00000000000"

    def test_url_wins_over_html(self) -> None:
        url = "https://opencode.ai/workspace/wrk_AAAAAAAAAAAAAAAAAAAAAAAAAA/go"
        html = "wrk_BBBBBBBBBBBBBBBBBBBBBBBBBB"
        assert _workspace_id_from(url, html) == "wrk_AAAAAAAAAAAAAAAAAAAAAAAAAA"

    def test_none_when_no_workspace_id_present(self) -> None:
        assert _workspace_id_from("https://opencode.ai/", "<html></html>") is None

    def test_rejects_a_malformed_workspace_id(self) -> None:
        # ULID body is 26 Crockford base32 chars; I/L/O/U are excluded. A
        # truncated or lowercase id must not be captured and silently shipped
        # into the Secret, where it looks exactly like an expired cookie.
        assert _workspace_id_from("https://opencode.ai/workspace/wrk_TOOSHORT/go") is None
        assert _workspace_id_from("wrk_01jtestwrkspace00000000000") is None
