"""Enrolment flows: official Claude Code login, Codex device code.

The dashboard implements neither OAuth flow (Plans 005 and 009), so what is
worth testing is the fail-closed staging swap (a failed ceremony never
displaces a working login) and the promise that no token is ever printed.
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

try:
    import fcntl  # noqa: F401
except ModuleNotFoundError as exc:  # these ceremonies run on Linux only
    pytest.skip(f"requires the POSIX locking backend: {exc}", allow_module_level=True)

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
    """Plan 009: the official login lands in a staging sibling of the
    account's persistent profile and is swapped in only once verified."""

    @staticmethod
    def _profile(tmp_path: Path, name: str = "claude-personal") -> Path:
        return tmp_path / name

    @staticmethod
    def _install_old_login(profile: Path, access: str = "old-access") -> None:
        profile.mkdir(mode=0o700)
        (profile / ".credentials.json").write_text(
            json.dumps(_credential_file(access=access, scopes=DEFAULT_SCOPES))
        )

    @staticmethod
    def _profile_access(profile: Path) -> str:
        raw = json.loads((profile / ".credentials.json").read_text())
        return str(raw["claudeAiOauth"]["accessToken"])

    def _run(
        self,
        tmp_path: Path,
        content: object | None = None,
        usage_side_effect: BaseException | None = None,
        account: str = "personal",
        profile: Path | None = None,
    ) -> tuple[Path, Callable[[str, str], None]]:
        profile = profile or self._profile(tmp_path)
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
            login_claude(account=account, config_dir=str(profile))
        return profile, fake_login

    def test_account_keys(self) -> None:
        assert CLAUDE_ACCOUNT_KEYS == {"personal": "claude", "work": "claude_work"}

    def test_default_profile_dirs_are_per_account(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR_PERSONAL", str(tmp_path / "p"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR_WORK", str(tmp_path / "w"))
        for account, access in (("personal", "access-personal"), ("work", "access-work")):
            fake = _writes(_credential_file(access=access, scopes=DEFAULT_SCOPES))
            with (
                patch("usage_dashboard.cli._run_official_claude_login", fake),
                patch("usage_dashboard.cli._claude_cli_version", return_value="2.1.276"),
                patch("usage_dashboard.cli.fetch_claude_usage"),
            ):
                login_claude(account=account)
        assert self._profile_access(tmp_path / "p") == "access-personal"
        assert self._profile_access(tmp_path / "w") == "access-work"

    def test_login_runs_in_a_staging_sibling(self, tmp_path: Path) -> None:
        profile, fake = self._run(tmp_path)
        staging = Path(fake.config_dir)  # type: ignore[attr-defined]
        # A sibling (same-filesystem rename), never the live profile itself.
        assert staging.parent == profile.parent
        assert staging != profile
        assert not staging.exists()  # renamed into place

    def test_the_verified_profile_stays(self, tmp_path: Path) -> None:
        # Plan 009 inverts Plan 005: the profile IS the credential, and its
        # CLI is the family's only refresher. Deleting it would sign out.
        profile, _ = self._run(tmp_path)
        assert self._profile_access(profile) == _ACCESS
        assert (profile.stat().st_mode & 0o777) == 0o700

    def test_no_token_store_is_written(self, tmp_path: Path) -> None:
        self._run(tmp_path)
        assert not (tmp_path / "tokens.json").exists()

    def test_reenrolment_replaces_the_old_login(self, tmp_path: Path) -> None:
        profile = self._profile(tmp_path)
        self._install_old_login(profile)
        self._run(tmp_path, profile=profile)
        assert self._profile_access(profile) == _ACCESS
        assert not (tmp_path / "claude-personal.prev").exists()

    def test_nothing_secret_is_printed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._run(tmp_path)
        output = capsys.readouterr()
        assert _ACCESS not in output.out + output.err
        assert _REFRESH not in output.out + output.err

    def _assert_failure_keeps_old_login(self, tmp_path: Path, **run: Any) -> None:
        profile = self._profile(tmp_path)
        self._install_old_login(profile)
        with pytest.raises(SystemExit) as exc:
            self._run(tmp_path, profile=profile, **run)
        assert exc.value.code == 1
        assert self._profile_access(profile) == "old-access"
        leftovers = [p.name for p in tmp_path.iterdir() if ".enrol-" in p.name]
        assert leftovers == [], "a failed ceremony left a second credential copy"

    def test_unknown_schema_fails_closed(self, tmp_path: Path) -> None:
        self._assert_failure_keeps_old_login(
            tmp_path, content={"someOtherShape": {"token": "x"}}
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
    def test_missing_token_keeps_the_old_login(
        self, tmp_path: Path, content: dict[str, Any]
    ) -> None:
        self._assert_failure_keeps_old_login(tmp_path, content=content)

    def test_missing_profile_scope_is_rejected(self, tmp_path: Path) -> None:
        # What `claude setup-token` produces: inference, but no user:profile.
        profile = self._profile(tmp_path)
        self._install_old_login(profile)
        fake = _writes(_credential_file(scopes=["user:inference"]))
        with (
            patch("usage_dashboard.cli._run_official_claude_login", fake),
            patch("usage_dashboard.cli._claude_cli_version", return_value="2.1.276"),
            patch("usage_dashboard.cli.fetch_claude_usage") as usage,
            pytest.raises(SystemExit),
        ):
            login_claude(account="personal", config_dir=str(profile))
        usage.assert_not_called()
        assert self._profile_access(profile) == "old-access"

    def test_absent_scope_metadata_still_validates_live(self, tmp_path: Path) -> None:
        # No scopes in the file: the live call is the only thing that can tell
        # whether this credential can read usage.
        profile = self._profile(tmp_path)
        fake = _writes(_credential_file(scopes=None))
        with (
            patch("usage_dashboard.cli._run_official_claude_login", fake),
            patch("usage_dashboard.cli._claude_cli_version", return_value="2.1.276"),
            patch("usage_dashboard.cli.fetch_claude_usage") as usage,
        ):
            login_claude(account="personal", config_dir=str(profile))
        usage.assert_called_once()
        assert self._profile_access(profile) == _ACCESS

    def test_403_during_validation_keeps_the_old_login(self, tmp_path: Path) -> None:
        self._assert_failure_keeps_old_login(
            tmp_path, usage_side_effect=FetchError("Claude usage forbidden: HTTP 403")
        )

    def test_a_failed_login_keeps_the_old_login(self, tmp_path: Path) -> None:
        profile = self._profile(tmp_path)
        self._install_old_login(profile)

        def fails(claude_bin: str, config_dir: str) -> None:
            raise ClaudeCredentialError("Claude Code login exited with status 1")

        with (
            patch("usage_dashboard.cli._run_official_claude_login", fails),
            patch("usage_dashboard.cli.fetch_claude_usage"),
            pytest.raises(SystemExit),
        ):
            login_claude(account="personal", config_dir=str(profile))
        assert self._profile_access(profile) == "old-access"
        assert [p.name for p in tmp_path.iterdir() if ".enrol-" in p.name] == []

    def test_a_cancel_mid_ceremony_keeps_the_old_login(self, tmp_path: Path) -> None:
        # The pane cancels with SIGTERM, which _raise_on_sigterm turns into
        # SystemExit; the staging copy must still be removed.
        profile = self._profile(tmp_path)
        self._install_old_login(profile)

        def cancelled(claude_bin: str, config_dir: str) -> None:
            Path(config_dir, ".credentials.json").write_text(
                json.dumps(_credential_file(scopes=DEFAULT_SCOPES))
            )
            raise SystemExit(143)

        with (
            patch("usage_dashboard.cli._run_official_claude_login", cancelled),
            patch("usage_dashboard.cli.fetch_claude_usage"),
            pytest.raises(SystemExit),
        ):
            login_claude(account="personal", config_dir=str(profile))
        assert self._profile_access(profile) == "old-access"
        assert [p.name for p in tmp_path.iterdir() if ".enrol-" in p.name] == []

    def test_enrolling_one_account_leaves_the_other_untouched(
        self, tmp_path: Path
    ) -> None:
        personal = self._profile(tmp_path, "claude-personal")
        work = self._profile(tmp_path, "claude-work")
        self._install_old_login(personal, access="personal-access")
        self._install_old_login(work, access="work-access")
        self._run(
            tmp_path,
            content=_credential_file(access="new-work", scopes=DEFAULT_SCOPES),
            account="work",
            profile=work,
        )
        assert self._profile_access(personal) == "personal-access"
        assert self._profile_access(work) == "new-work"

    def test_unknown_account_selector_exits(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as exc:
            login_claude(account="nope", config_dir=str(tmp_path / "x"))
        assert exc.value.code == 1

    def test_sigterm_handler_is_restored(self, tmp_path: Path) -> None:
        # The swap ignores SIGTERM briefly; a leaked SIG_IGN would be
        # inherited by every child this process later spawns.
        import signal

        before = signal.getsignal(signal.SIGTERM)
        self._run(tmp_path)
        assert signal.getsignal(signal.SIGTERM) is before
        with pytest.raises(SystemExit):
            self._run(tmp_path, content={"someOtherShape": {}})
        assert signal.getsignal(signal.SIGTERM) is before


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
