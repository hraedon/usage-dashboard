"""Enrolment flows: official Claude Code login import, Codex device code.

These replace the custom-PKCE tests deleted in Plan 005. The dashboard no
longer implements either OAuth flow, so what is worth testing is the
*importer's* fail-closed behaviour and the promise that no token is ever
printed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

import pytest

from usage_dashboard.claude_credentials import ClaudeCredentialError
from usage_dashboard.cli import CLAUDE_ACCOUNT_KEYS, login_claude, login_codex
from usage_dashboard.server.fetch_types import FetchError
from usage_dashboard.server.token_store import TokenStore

_ACCESS = "sk-ant-oat-ACCESSTOKENVALUE"
_REFRESH = "sk-ant-ort-REFRESHTOKENVALUE"


def _credential_file(
    access: str = _ACCESS,
    refresh: str = _REFRESH,
    scopes: list[str] | None = None,
) -> dict[str, Any]:
    """The schema Claude Code 2.1.276 writes (captured from a real login)."""
    payload: dict[str, Any] = {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresAt": 1789832809000,
        "refreshTokenExpiresAt": 1792424809000,
        "subscriptionType": "pro",
        "rateLimitTier": "default_claude_ai",
    }
    if scopes is not None:
        payload["scopes"] = scopes
    return {"claudeAiOauth": payload}


def _writes(content: object | None) -> Callable[[str, str], None]:
    """A fake official login that drops *content* into the config dir."""

    def fake(claude_bin: str, config_dir: str) -> None:
        if content is not None:
            path = Path(config_dir) / ".credentials.json"
            path.write_text(
                content if isinstance(content, str) else json.dumps(content)
            )
        fake.config_dir = config_dir  # type: ignore[attr-defined]

    return fake


DEFAULT_SCOPES = ["user:inference", "user:profile", "user:sessions:claude_code"]


class TestClaudeEnrolment:
    def _run(
        self,
        tmp_path: Path,
        content: object | None = None,
        usage_side_effect: BaseException | None = None,
        account: str = "personal",
    ) -> tuple[TokenStore, Callable[[str, str], None]]:
        store_path = tmp_path / "tokens.json"
        fake_login = _writes(
            content if content is not None else _credential_file(scopes=DEFAULT_SCOPES)
        )
        with (
            patch("usage_dashboard.cli._run_official_claude_login", fake_login),
            patch("usage_dashboard.cli._claude_cli_version", return_value="2.1.276"),
            patch(
                "usage_dashboard.cli.fetch_claude_usage",
                side_effect=usage_side_effect,
            ),
        ):
            login_claude(account=account, token_store_path=str(store_path))
        return TokenStore(store_path), fake_login

    def test_personal_and_work_map_to_separate_keys(self, tmp_path: Path) -> None:
        assert CLAUDE_ACCOUNT_KEYS == {"personal": "claude", "work": "claude_work"}

        store_path = tmp_path / "tokens.json"
        for account, access in (("personal", "access-personal"), ("work", "access-work")):
            fake = _writes(_credential_file(access=access, scopes=DEFAULT_SCOPES))
            with (
                patch("usage_dashboard.cli._run_official_claude_login", fake),
                patch("usage_dashboard.cli._claude_cli_version", return_value="2.1.276"),
                patch("usage_dashboard.cli.fetch_claude_usage"),
            ):
                login_claude(account=account, token_store_path=str(store_path))

        store = TokenStore(store_path)
        assert store.get("claude")[0] == "access-personal"
        assert store.get("claude_work")[0] == "access-work"

    def test_known_schema_imports(self, tmp_path: Path) -> None:
        store, _ = self._run(tmp_path)
        assert store.get("claude") == (_ACCESS, _REFRESH)
        assert store.get_metadata("claude", "subscription_type") == "pro"
        assert store.get_metadata("claude", "scopes") == DEFAULT_SCOPES

    def test_no_client_id_is_stored_when_the_file_has_none(self, tmp_path: Path) -> None:
        # Claude Code 2.1.276 emits no clientId, and refresh_claude_token omits
        # client_id when it is None — so storing a guessed one would be wrong.
        store, _ = self._run(tmp_path)
        assert store.get_metadata("claude", "client_id") is None

    def test_temporary_credential_store_is_removed(self, tmp_path: Path) -> None:
        _, fake = self._run(tmp_path)
        config_dir = Path(fake.config_dir)  # type: ignore[attr-defined]
        # Leaving this behind would give the credential family a second
        # refresher and re-create the WI-001 rotation fight.
        assert not config_dir.exists()

    def test_nothing_secret_is_printed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._run(tmp_path)
        output = capsys.readouterr()
        assert _ACCESS not in output.out + output.err
        assert _REFRESH not in output.out + output.err

    def test_unknown_schema_fails_closed_and_names_the_version(
        self, tmp_path: Path
    ) -> None:
        store_path = tmp_path / "tokens.json"
        TokenStore(store_path).save("claude", "existing-access", "existing-refresh")
        fake = _writes({"someOtherShape": {"token": "x"}})
        with (
            patch("usage_dashboard.cli._run_official_claude_login", fake),
            patch("usage_dashboard.cli._claude_cli_version", return_value="2.1.276"),
            patch("usage_dashboard.cli.fetch_claude_usage"),
            pytest.raises(SystemExit) as exc,
        ):
            login_claude(account="personal", token_store_path=str(store_path))
        assert exc.value.code == 1
        assert TokenStore(store_path).get("claude") == (
            "existing-access",
            "existing-refresh",
        )

    def test_error_text_names_the_cli_version(self) -> None:
        from usage_dashboard.claude_credentials import parse_credentials

        with pytest.raises(ClaudeCredentialError) as exc:
            parse_credentials({"nope": 1}, cli_version="2.1.276")
        assert "2.1.276" in str(exc.value)

    @pytest.mark.parametrize(
        "content",
        [
            {"claudeAiOauth": {"refreshToken": _REFRESH}},  # no access token
            {"claudeAiOauth": {"accessToken": _ACCESS}},  # no refresh token
        ],
    )
    def test_missing_token_leaves_the_existing_entry(
        self, tmp_path: Path, content: dict[str, Any]
    ) -> None:
        store_path = tmp_path / "tokens.json"
        TokenStore(store_path).save("claude", "old-access", "old-refresh")
        fake = _writes(content)
        with (
            patch("usage_dashboard.cli._run_official_claude_login", fake),
            patch("usage_dashboard.cli._claude_cli_version", return_value="2.1.276"),
            patch("usage_dashboard.cli.fetch_claude_usage"),
            pytest.raises(SystemExit),
        ):
            login_claude(account="personal", token_store_path=str(store_path))
        assert TokenStore(store_path).get("claude") == ("old-access", "old-refresh")

    def test_missing_profile_scope_is_rejected(self, tmp_path: Path) -> None:
        store_path = tmp_path / "tokens.json"
        TokenStore(store_path).save("claude", "old-access", "old-refresh")
        # What `claude setup-token` produces: inference, but no user:profile.
        fake = _writes(_credential_file(scopes=["user:inference"]))
        with (
            patch("usage_dashboard.cli._run_official_claude_login", fake),
            patch("usage_dashboard.cli._claude_cli_version", return_value="2.1.276"),
            patch("usage_dashboard.cli.fetch_claude_usage") as usage,
            pytest.raises(SystemExit),
        ):
            login_claude(account="personal", token_store_path=str(store_path))
        usage.assert_not_called()
        assert TokenStore(store_path).get("claude") == ("old-access", "old-refresh")

    def test_absent_scope_metadata_still_validates_live(self, tmp_path: Path) -> None:
        # No scopes in the file: the live call is the only thing that can tell
        # whether this credential can read usage.
        store_path = tmp_path / "tokens.json"
        fake = _writes(_credential_file(scopes=None))
        with (
            patch("usage_dashboard.cli._run_official_claude_login", fake),
            patch("usage_dashboard.cli._claude_cli_version", return_value="2.1.276"),
            patch("usage_dashboard.cli.fetch_claude_usage") as usage,
        ):
            login_claude(account="personal", token_store_path=str(store_path))
        usage.assert_called_once()
        assert TokenStore(store_path).get("claude") == (_ACCESS, _REFRESH)

    def test_403_during_validation_leaves_the_existing_entry(
        self, tmp_path: Path
    ) -> None:
        store_path = tmp_path / "tokens.json"
        TokenStore(store_path).save("claude", "old-access", "old-refresh")
        fake = _writes(_credential_file(scopes=DEFAULT_SCOPES))
        with (
            patch("usage_dashboard.cli._run_official_claude_login", fake),
            patch("usage_dashboard.cli._claude_cli_version", return_value="2.1.276"),
            patch(
                "usage_dashboard.cli.fetch_claude_usage",
                side_effect=FetchError("Claude usage forbidden: HTTP 403"),
            ),
            pytest.raises(SystemExit),
        ):
            login_claude(account="personal", token_store_path=str(store_path))
        assert TokenStore(store_path).get("claude") == ("old-access", "old-refresh")

    def test_a_failed_login_leaves_the_existing_entry(self, tmp_path: Path) -> None:
        store_path = tmp_path / "tokens.json"
        TokenStore(store_path).save("claude", "old-access", "old-refresh")

        def fails(claude_bin: str, config_dir: str) -> None:
            raise ClaudeCredentialError("Claude Code login exited with status 1")

        with (
            patch("usage_dashboard.cli._run_official_claude_login", fails),
            patch("usage_dashboard.cli.fetch_claude_usage"),
            pytest.raises(SystemExit),
        ):
            login_claude(account="personal", token_store_path=str(store_path))
        assert TokenStore(store_path).get("claude") == ("old-access", "old-refresh")

    def test_enrolling_one_account_leaves_the_other_untouched(
        self, tmp_path: Path
    ) -> None:
        store_path = tmp_path / "tokens.json"
        store = TokenStore(store_path)
        store.save("claude", "personal-access", "personal-refresh")
        store.save("claude_work", "work-access", "work-refresh")

        fake = _writes(_credential_file(access="new-work", scopes=DEFAULT_SCOPES))
        with (
            patch("usage_dashboard.cli._run_official_claude_login", fake),
            patch("usage_dashboard.cli._claude_cli_version", return_value="2.1.276"),
            patch("usage_dashboard.cli.fetch_claude_usage"),
        ):
            login_claude(account="work", token_store_path=str(store_path))

        reloaded = TokenStore(store_path)
        assert reloaded.get("claude") == ("personal-access", "personal-refresh")
        assert reloaded.get("claude_work")[0] == "new-work"

    def test_unknown_account_selector_exits(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as exc:
            login_claude(account="nope", token_store_path=str(tmp_path / "t.json"))
        assert exc.value.code == 1


class _FakeCodexClient:
    """Stand-in for CodexAppServerClient in the enrolment CLI."""

    def __init__(self, *, login_error: Exception | None = None,
                 verify_error: Exception | None = None) -> None:
        self.login_error = login_error
        self.verify_error = verify_error
        self.closed = 0
        self.verified = False

    def device_code_login(
        self, present_code: Callable[[str, str], None], timeout: float = 900.0
    ) -> None:
        if self.login_error is not None:
            raise self.login_error
        present_code("https://auth.openai.com/codex/device", "L1G2-RW770")

    def read_account_and_rate_limits(self) -> tuple[dict[str, Any], dict[str, Any]]:
        if self.verify_error is not None:
            raise self.verify_error
        self.verified = True
        return (
            {"account": {"type": "chatgpt", "planType": "pro"}},
            {"rateLimitsByLimitId": {"codex": {"primary": {"usedPercent": 93}}}},
        )

    def close(self) -> None:
        self.closed += 1


class TestCodexEnrolment:
    def _patch(self, clients: list[_FakeCodexClient]) -> Any:
        created = iter(clients)
        return patch(
            "usage_dashboard.cli.CodexAppServerClient",
            side_effect=lambda *a, **k: next(created),
        )

    def test_prints_the_code_and_verifies(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        login, verify = _FakeCodexClient(), _FakeCodexClient()
        with self._patch([login, verify]):
            login_codex(codex_home=str(tmp_path), codex_bin="codex")
        out = capsys.readouterr().out
        assert "https://auth.openai.com/codex/device" in out
        assert "L1G2-RW770" in out
        # Verification runs against a *fresh* client, which is what proves the
        # credential actually reached CODEX_HOME on disk.
        assert verify.verified
        assert login.closed == 1 and verify.closed == 1

    def test_no_token_material_is_printed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with self._patch([_FakeCodexClient(), _FakeCodexClient()]):
            login_codex(codex_home=str(tmp_path), codex_bin="codex")
        out = capsys.readouterr().out.lower()
        for forbidden in ("access_token", "refresh_token", "bearer ", "eyj"):
            assert forbidden not in out

    def test_login_failure_exits_nonzero_and_closes(self, tmp_path: Path) -> None:
        login = _FakeCodexClient(login_error=FetchError("device code expired"))
        with self._patch([login]), pytest.raises(SystemExit) as exc:
            login_codex(codex_home=str(tmp_path), codex_bin="codex")
        assert exc.value.code == 1
        assert login.closed == 1

    def test_verification_failure_exits_nonzero(self, tmp_path: Path) -> None:
        verify = _FakeCodexClient(verify_error=FetchError("still not authenticated"))
        with (
            self._patch([_FakeCodexClient(), verify]),
            pytest.raises(SystemExit) as exc,
        ):
            login_codex(codex_home=str(tmp_path), codex_bin="codex")
        assert exc.value.code == 1
        assert verify.closed == 1
