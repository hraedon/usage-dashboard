from __future__ import annotations

from pathlib import Path

from usage_dashboard.server.main import _resolve_claude_tokens, _resolve_cookie
from usage_dashboard.server.token_store import TokenStore


class TestResolveClaudeTokens:
    def test_first_boot_seeds_from_env(self, tmp_path: Path) -> None:
        store = TokenStore(tmp_path / "tokens.json")
        access, refresh = _resolve_claude_tokens(store, "a0", "r0")
        assert (access, refresh) == ("a0", "r0")
        assert store.load_claude_tokens() == ("a0", "r0")
        assert store.get_claude_seed_marker() is not None

    def test_restart_keeps_refreshed_tokens(self, tmp_path: Path) -> None:
        # WI-001 regression: after a refresh, a restart with the same (stale)
        # Secret must NOT clobber the refreshed tokens.
        path = tmp_path / "tokens.json"
        store = TokenStore(path)
        _resolve_claude_tokens(store, "a0", "r0")  # first boot
        store.save_claude_tokens("a1", "r1")  # scheduler refreshed

        restarted = TokenStore(path)  # new process, same PVC
        access, refresh = _resolve_claude_tokens(restarted, "a0", "r0")
        assert (access, refresh) == ("a1", "r1")

    def test_changed_secret_is_adopted(self, tmp_path: Path) -> None:
        # A deliberate re-login updates the Secret; the new pair must win.
        path = tmp_path / "tokens.json"
        store = TokenStore(path)
        _resolve_claude_tokens(store, "a0", "r0")
        store.save_claude_tokens("a1", "r1")

        restarted = TokenStore(path)
        access, refresh = _resolve_claude_tokens(restarted, "a2", "r2")
        assert (access, refresh) == ("a2", "r2")
        assert restarted.load_claude_tokens() == ("a2", "r2")

    def test_empty_env_uses_persisted(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        store = TokenStore(path)
        store.save_claude_tokens("a1", "r1")
        access, refresh = _resolve_claude_tokens(store, None, None)
        assert (access, refresh) == ("a1", "r1")

    def test_empty_env_and_empty_store(self, tmp_path: Path) -> None:
        store = TokenStore(tmp_path / "tokens.json")
        assert _resolve_claude_tokens(store, None, None) == (None, None)


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
