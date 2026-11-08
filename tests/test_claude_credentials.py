"""Fixture tests for the quarantined Claude Code credential parser.

The schema is not a documented interface, so the parser's job is to accept
exactly what a known CLI version writes and fail closed — loudly, naming the
version — on anything else.
"""
from __future__ import annotations

from typing import Any

import pytest

from usage_dashboard.claude_credentials import (
    REQUIRED_SCOPE,
    ClaudeCredentialError,
    parse_credentials,
)

# Verbatim shape written by `claude auth login` on Linux, Claude Code 2.1.276.
FIXTURE_2_1_276: dict[str, Any] = {
    "claudeAiOauth": {
        "accessToken": "sk-ant-oat01-EXAMPLE",
        "refreshToken": "sk-ant-ort01-EXAMPLE",
        "expiresAt": 1789832809000,
        "refreshTokenExpiresAt": 1792424809000,
        "scopes": [
            "user:file_upload",
            "user:inference",
            "user:mcp_servers",
            "user:profile",
            "user:sessions:claude_code",
        ],
        "subscriptionType": "pro",
        "rateLimitTier": "default_claude_ai",
    }
}


class TestKnownSchema:
    def test_parses_the_2_1_276_fixture(self) -> None:
        cred = parse_credentials(FIXTURE_2_1_276, cli_version="2.1.276")
        assert cred.access_token == "sk-ant-oat01-EXAMPLE"
        assert cred.refresh_token == "sk-ant-ort01-EXAMPLE"
        assert cred.expires_at == 1789832809000
        assert cred.subscription_type == "pro"
        assert cred.has_profile_scope
        assert cred.scopes_known

    def test_a_cli_refreshed_file_still_parses(self) -> None:
        # After the CLI refreshes (Plan 009 WP-1, verified on 2.1.276) the
        # file gains nothing the parser must understand; extra keys are
        # ignored rather than rejected.
        raw = {"claudeAiOauth": dict(FIXTURE_2_1_276["claudeAiOauth"], clientId="abc")}
        assert parse_credentials(raw).access_token == "sk-ant-oat01-EXAMPLE"

    def test_a_minimal_file_parses(self) -> None:
        # The CLI accepts (and then refreshes) a file carrying only the pair,
        # expiry, scopes and plan — the migration's shape.
        raw = {
            "claudeAiOauth": {
                "accessToken": "a",
                "refreshToken": "r",
                "expiresAt": 1,
                "scopes": [REQUIRED_SCOPE],
                "subscriptionType": "max",
            }
        }
        cred = parse_credentials(raw)
        assert cred.expires_at == 1
        assert cred.has_profile_scope

    def test_absent_scopes_are_allowed_but_not_claimed(self) -> None:
        raw = {
            "claudeAiOauth": {
                "accessToken": "a",
                "refreshToken": "r",
            }
        }
        cred = parse_credentials(raw)
        assert cred.scopes == ()
        assert not cred.scopes_known
        assert not cred.has_profile_scope

    def test_setup_token_style_scopes_lack_profile(self) -> None:
        raw = {
            "claudeAiOauth": {
                "accessToken": "a",
                "refreshToken": "r",
                "scopes": ["user:inference"],
            }
        }
        cred = parse_credentials(raw)
        assert cred.scopes_known
        assert not cred.has_profile_scope


class TestFailsClosed:
    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("a string", id="not-an-object"),
            pytest.param([], id="a-list"),
            pytest.param({}, id="empty"),
            pytest.param({"oauth": {}}, id="wrong-top-level-key"),
            pytest.param({"claudeAiOauth": "nope"}, id="oauth-not-an-object"),
            pytest.param({"claudeAiOauth": {"refreshToken": "r"}}, id="no-access"),
            pytest.param({"claudeAiOauth": {"accessToken": "a"}}, id="no-refresh"),
            pytest.param(
                {"claudeAiOauth": {"accessToken": "", "refreshToken": "r"}},
                id="empty-access",
            ),
            pytest.param(
                {"claudeAiOauth": {"accessToken": 1, "refreshToken": "r"}},
                id="access-not-a-string",
            ),
            pytest.param(
                {"claudeAiOauth": {"accessToken": "a", "refreshToken": "r",
                                   "scopes": "user:profile"}},
                id="scopes-not-a-list",
            ),
            pytest.param(
                {"claudeAiOauth": {"accessToken": "a", "refreshToken": "r",
                                   "scopes": [1, 2]}},
                id="scopes-not-strings",
            ),
            pytest.param(
                {"claudeAiOauth": {"accessToken": "a", "refreshToken": "r",
                                   "expiresAt": "soon"}},
                id="expiry-not-an-int",
            ),
        ],
    )
    def test_unknown_shapes_raise(self, raw: object) -> None:
        with pytest.raises(ClaudeCredentialError):
            parse_credentials(raw, cli_version="2.1.276")

    def test_the_error_names_the_installed_version(self) -> None:
        with pytest.raises(ClaudeCredentialError) as exc:
            parse_credentials({"unexpected": True}, cli_version="9.9.9")
        message = str(exc.value)
        assert "9.9.9" in message
        assert "claude_credentials.py" in message

    def test_an_unknown_version_is_still_reported(self) -> None:
        with pytest.raises(ClaudeCredentialError) as exc:
            parse_credentials({"unexpected": True})
        assert "unknown" in str(exc.value)
