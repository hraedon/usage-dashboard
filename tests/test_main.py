from __future__ import annotations

import json
from pathlib import Path

import pytest

from usage_dashboard.server.claude_profile import ClaudeProfileConfig
from usage_dashboard.server.main import _resolve_claude_profile, _resolve_cookie
from usage_dashboard.server.token_store import TokenStore


class TestResolveClaudeProfile:
    """Plan 009: an account is configured exactly when its CLI profile holds
    a credential; the token store no longer carries Claude OAuth tokens."""

    @staticmethod
    def _enrol(profile: Path, refresh: str = "cli-refresh") -> None:
        profile.mkdir(parents=True)
        (profile / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": refresh}})
        )

    @staticmethod
    def _point_at(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR_PERSONAL", str(tmp_path / "personal"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR_WORK", str(tmp_path / "work"))

    def test_no_profile_means_unconfigured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._point_at(monkeypatch, tmp_path)
        store = TokenStore(tmp_path / "tokens.json")
        assert _resolve_claude_profile(store, "claude", "claude") is None
        assert _resolve_claude_profile(store, "claude_work", "claude") is None

    def test_an_enrolled_profile_is_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._point_at(monkeypatch, tmp_path)
        self._enrol(tmp_path / "work")
        store = TokenStore(tmp_path / "tokens.json")
        profile = _resolve_claude_profile(store, "claude_work", "/opt/claude")
        assert profile == ClaudeProfileConfig(
            config_dir=str(tmp_path / "work"), binary="/opt/claude"
        )
        assert _resolve_claude_profile(store, "claude", "claude") is None

    def test_an_unmigrated_account_stays_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A pre-Plan-009 pair with no profile is never used (that would make
        # the dashboard a refresher again), but the account must not vanish
        # either: it stays configured, so its tile reads "login required".
        self._point_at(monkeypatch, tmp_path)
        store = TokenStore(tmp_path / "tokens.json")
        store.save("claude", "old-access", "old-refresh")
        profile = _resolve_claude_profile(store, "claude", "claude")
        assert profile == ClaudeProfileConfig(config_dir=str(tmp_path / "personal"))
        assert store.get("claude") == ("old-access", "old-refresh")

    def test_the_legacy_entry_survives_until_the_cli_rotates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Freshly migrated: the profile still holds the store's refresh
        # token, so the store copy is the only fallback. Keep it.
        self._point_at(monkeypatch, tmp_path)
        self._enrol(tmp_path / "personal", refresh="old-refresh")
        store = TokenStore(tmp_path / "tokens.json")
        store.save("claude", "old-access", "old-refresh")
        assert _resolve_claude_profile(store, "claude", "claude") is not None
        assert store.get("claude") == ("old-access", "old-refresh")

    def test_the_legacy_entry_is_pruned_once_the_cli_has_rotated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._point_at(monkeypatch, tmp_path)
        self._enrol(tmp_path / "personal", refresh="rotated-by-cli")
        store = TokenStore(tmp_path / "tokens.json")
        store.save("claude", "old-access", "old-refresh")
        store.set_seed_marker("claude", "m")
        store.save_credential("ollama", "cookie")
        assert _resolve_claude_profile(store, "claude", "claude") is not None
        assert not store.has_entry("claude")
        assert store.get_credential("ollama") == "cookie"

    def test_an_unreadable_profile_keeps_the_legacy_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._point_at(monkeypatch, tmp_path)
        (tmp_path / "personal").mkdir()
        (tmp_path / "personal" / ".credentials.json").write_text("{broken")
        store = TokenStore(tmp_path / "tokens.json")
        store.save("claude", "old-access", "old-refresh")
        _resolve_claude_profile(store, "claude", "claude")
        assert store.has_entry("claude")

    def test_an_interrupted_swap_is_settled_at_startup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Enrolment died between its two renames, then the pod restarted:
        # without recovery the profile looks absent and the account drops.
        self._point_at(monkeypatch, tmp_path)
        self._enrol(tmp_path / "personal.prev")
        (tmp_path / "personal.swapping").write_text("")
        store = TokenStore(tmp_path / "tokens.json")
        assert _resolve_claude_profile(store, "claude", "claude") is not None
        assert (tmp_path / "personal" / ".credentials.json").exists()


class TestResolveCookie:
    def test_first_boot_seeds_from_env(self, tmp_path: Path) -> None:
        store = TokenStore(tmp_path / "tokens.json")
        assert _resolve_cookie(store, "cookie-0", "ollama") == "cookie-0"
        assert store.get_credential("ollama") == "cookie-0"
        assert store.get_seed_marker("ollama") is not None

    def test_restart_keeps_persisted_cookie(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        store = TokenStore(path)
        _resolve_cookie(store, "cookie-0", "ollama")
        store.save_credential("ollama", "cookie-1")

        restarted = TokenStore(path)
        assert _resolve_cookie(restarted, "cookie-0", "ollama") == "cookie-1"

    def test_changed_secret_is_adopted(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        store = TokenStore(path)
        _resolve_cookie(store, "cookie-0", "ollama")
        store.save_credential("ollama", "cookie-1")

        restarted = TokenStore(path)
        assert _resolve_cookie(restarted, "cookie-2", "ollama") == "cookie-2"
        assert restarted.get_credential("ollama") == "cookie-2"

    def test_empty_env_uses_persisted(self, tmp_path: Path) -> None:
        store = TokenStore(tmp_path / "tokens.json")
        store.save_credential("ollama", "cookie-1")
        assert _resolve_cookie(store, None, "ollama") == "cookie-1"

    def test_empty_env_and_empty_store_returns_none(self, tmp_path: Path) -> None:
        store = TokenStore(tmp_path / "tokens.json")
        assert _resolve_cookie(store, None, "ollama") is None

    def test_store_keys_are_namespaced_per_provider(self, tmp_path: Path) -> None:
        # Two cookie-authenticated providers must not share a slot: seeding one
        # cannot leak into the other, and re-seeding one cannot evict the other.
        path = tmp_path / "tokens.json"
        store = TokenStore(path)
        assert _resolve_cookie(store, "ollama-0", "ollama") == "ollama-0"
        assert _resolve_cookie(store, "oc-0", "opencode") == "oc-0"
        assert store.get_credential("ollama") == "ollama-0"
        assert store.get_credential("opencode") == "oc-0"

        restarted = TokenStore(path)
        assert _resolve_cookie(restarted, "ollama-1", "ollama") == "ollama-1"
        assert _resolve_cookie(restarted, "oc-0", "opencode") == "oc-0"


class TestAntigravityMode:
    """Plan 007: ``ANTIGRAVITY_MODE=cli`` is the only enabled mode."""

    def test_cli_mode_builds_the_config(self) -> None:
        from usage_dashboard.server.main import _build_antigravity_config

        config = _build_antigravity_config("cli", "/data/antigravity", "/usr/local/bin/agy")
        assert config is not None
        assert config.home == "/data/antigravity"
        assert config.binary == "/usr/local/bin/agy"

    def test_disabled_and_unknown_modes_leave_it_unconfigured(self, caplog) -> None:
        from usage_dashboard.server.main import _build_antigravity_config

        assert _build_antigravity_config("disabled", "/h", "agy") is None
        assert _build_antigravity_config("", "/h", "agy") is None
        assert _build_antigravity_config("app_server", "/h", "agy") is None
        assert "Unknown ANTIGRAVITY_MODE" in caplog.text
