from __future__ import annotations

import json
from pathlib import Path

from usage_dashboard.server.token_store import TokenStore


class TestTokenStore:
    def test_get_returns_none_for_missing_provider(self, tmp_path: Path) -> None:
        store = TokenStore(tmp_path / "tokens.json")
        assert store.get("claude") == (None, None)

    def test_save_and_get(self, tmp_path: Path) -> None:
        store = TokenStore(tmp_path / "tokens.json")
        store.save("claude", "access-123", "refresh-456")
        assert store.get("claude") == ("access-123", "refresh-456")

    def test_save_overwrites_existing(self, tmp_path: Path) -> None:
        store = TokenStore(tmp_path / "tokens.json")
        store.save("claude", "old-access", "old-refresh")
        store.save("claude", "new-access", "new-refresh")
        assert store.get("claude") == ("new-access", "new-refresh")

    def test_multiple_providers(self, tmp_path: Path) -> None:
        store = TokenStore(tmp_path / "tokens.json")
        store.save("claude", "c-access", "c-refresh")
        store.save("zai", "z-access", "z-refresh")
        assert store.get("claude") == ("c-access", "c-refresh")
        assert store.get("zai") == ("z-access", "z-refresh")

    def test_persistence_across_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        store1 = TokenStore(path)
        store1.save("claude", "access", "refresh")
        store2 = TokenStore(path)
        assert store2.get("claude") == ("access", "refresh")

    def test_load_claude_tokens_convenience(self, tmp_path: Path) -> None:
        store = TokenStore(tmp_path / "tokens.json")
        store.save_claude_tokens("my-access", "my-refresh")
        assert store.load_claude_tokens() == ("my-access", "my-refresh")

    def test_load_from_missing_file(self, tmp_path: Path) -> None:
        store = TokenStore(tmp_path / "nonexistent.json")
        assert store.get("claude") == (None, None)

    def test_load_from_corrupt_json(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        path.write_text("not valid json{{{")
        store = TokenStore(path)
        assert store.get("claude") == (None, None)

    def test_file_contents_are_valid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        store = TokenStore(path)
        store.save("claude", "acc", "ref")
        with open(path) as f:
            data = json.load(f)
        assert data["claude"]["access_token"] == "acc"
        assert data["claude"]["refresh_token"] == "ref"


class TestSeedMarker:
    def test_marker_defaults_to_none(self, tmp_path: Path) -> None:
        store = TokenStore(tmp_path / "tokens.json")
        assert store.get_claude_seed_marker() is None

    def test_set_and_get_marker(self, tmp_path: Path) -> None:
        store = TokenStore(tmp_path / "tokens.json")
        store.set_claude_seed_marker("marker-abc")
        assert store.get_claude_seed_marker() == "marker-abc"

    def test_save_preserves_marker(self, tmp_path: Path) -> None:
        # A token refresh (save) must not wipe the env-seed marker.
        store = TokenStore(tmp_path / "tokens.json")
        store.save_claude_tokens("a0", "r0")
        store.set_claude_seed_marker("marker-0")
        store.save_claude_tokens("a1", "r1")  # simulate a refresh
        assert store.get_claude_seed_marker() == "marker-0"
        assert store.load_claude_tokens() == ("a1", "r1")

    def test_marker_persists_across_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        TokenStore(path).set_claude_seed_marker("m")
        assert TokenStore(path).get_claude_seed_marker() == "m"
