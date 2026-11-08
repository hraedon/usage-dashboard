"""The one-off Plan 009 migration script (scripts/migrate_claude_store_to_profile.py).

It runs once, in production, inside the new pod; a bug in it costs a
re-login or worse, so it gets the same fail-closed scrutiny as enrolment.
"""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

try:
    import fcntl  # noqa: F401
except ModuleNotFoundError as exc:  # runs in the Linux pod only
    pytest.skip(f"requires the POSIX locking backend: {exc}", allow_module_level=True)

from usage_dashboard.claude_credentials import parse_credentials
from usage_dashboard.server.fetch_types import FetchAuthError, FetchError
from usage_dashboard.server.token_store import TokenStore

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "migrate_claude_store_to_profile.py"


@pytest.fixture()
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migrate_claude", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TOKEN_STORE_PATH", str(tmp_path / "tokens.json"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR_PERSONAL", str(tmp_path / "claude-personal"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR_WORK", str(tmp_path / "claude-work"))
    monkeypatch.delenv("API_KEY", raising=False)
    return tmp_path


def _seed(tmp_path: Path, expires_at: int | None) -> TokenStore:
    store = TokenStore(tmp_path / "tokens.json")
    metadata: dict[str, object] = {"scopes": ["user:profile"], "subscription_type": "max"}
    if expires_at is not None:
        metadata["expires_at"] = expires_at
    store.save("claude", "acc", "ref", metadata=metadata)
    store.save_credential("ollama", "cookie")
    return store


def _profile(tmp_path: Path) -> dict[str, object]:
    raw = json.loads((tmp_path / "claude-personal" / ".credentials.json").read_text())
    assert isinstance(raw, dict)
    return raw


def test_a_live_pair_moves_into_the_profile(script: ModuleType, env: Path) -> None:
    future = int(time.time() * 1000) + 3600_000
    store = _seed(env, future)
    with patch.object(script, "fetch_claude_usage") as usage:
        script.main()
    usage.assert_called_once()
    raw = _profile(env)
    cred = parse_credentials(raw)
    assert (cred.access_token, cred.refresh_token, cred.expires_at) == ("acc", "ref", future)
    assert cred.has_profile_scope and cred.subscription_type == "max"
    profile = env / "claude-personal"
    assert (profile.stat().st_mode & 0o777) == 0o700
    assert ((profile / ".credentials.json").stat().st_mode & 0o777) == 0o600
    # The store copy stays until the CLI's first refresh proves the profile.
    assert store.get("claude") == ("acc", "ref")
    assert store.get_credential("ollama") == "cookie"


def test_a_rejected_access_token_is_written_as_expired(
    script: ModuleType, env: Path
) -> None:
    _seed(env, int(time.time() * 1000) + 3600_000)
    with patch.object(script, "fetch_claude_usage", side_effect=FetchAuthError("401")):
        script.main()
    assert parse_credentials(_profile(env)).expires_at == 0


def test_an_unknown_or_past_expiry_skips_the_check(script: ModuleType, env: Path) -> None:
    _seed(env, None)
    with patch.object(script, "fetch_claude_usage") as usage:
        script.main()
    usage.assert_not_called()
    assert parse_credentials(_profile(env)).expires_at == 0


def test_a_transient_check_failure_changes_nothing(
    script: ModuleType, env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _seed(env, int(time.time() * 1000) + 3600_000)
    with patch.object(script, "fetch_claude_usage", side_effect=FetchError("HTTP 500")):
        script.main()
    assert not (env / "claude-personal").exists()
    assert [p.name for p in env.iterdir() if ".enrol-" in p.name] == []
    assert store.get("claude") == ("acc", "ref")
    assert "not migrated" in capsys.readouterr().out


def test_it_is_idempotent_and_never_overwrites_a_profile(
    script: ModuleType, env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(env, None)
    script.main()
    (env / "claude-personal" / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "cli", "refreshToken": "cli-r"}})
    )
    script.main()
    assert parse_credentials(_profile(env)).access_token == "cli"
    assert "already holds a credential" in capsys.readouterr().out


def test_no_token_material_is_printed(
    script: ModuleType, env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = TokenStore(env / "tokens.json")
    store.save("claude", "sk-ant-oat-SECRET", "sk-ant-ort-SECRET")
    script.main()
    out = capsys.readouterr()
    assert "SECRET" not in out.out + out.err


def test_it_asks_the_server_to_fetch_after_migrating(
    script: ModuleType, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("PORT", "9999")
    _seed(env, None)
    with patch.object(script.httpx, "post") as post:
        post.return_value.status_code = 200
        script.main()
    url = post.call_args.args[0]
    assert url == "http://127.0.0.1:9999/refresh"
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer k"}
