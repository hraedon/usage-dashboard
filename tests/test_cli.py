"""Cookie-scraping login helpers for Ollama and OpenCode Go.

The Claude and Codex PKCE tests that used to live here went with the flows
themselves in Plan 005; enrolment is now covered by test_cli_enrolment.py.
"""
from __future__ import annotations

from usage_dashboard.cli import (
    _ollama_cookies,
    _opencode_auth_cookie,
    _serialize_cookie_header,
    _workspace_id_from,
)


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
