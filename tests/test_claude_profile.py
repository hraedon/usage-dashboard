"""Claude credentials owned by the official CLI (Plan 009).

The real ``claude`` binary is replaced by a stub script. Its behaviour models
what WP-1 verified against Claude Code 2.1.276 in the server pod:
``claude -p /usage`` is a zero-turn local command, and with an expired access
token the CLI refreshes first and rewrites ``.credentials.json`` atomically.
The usage endpoint itself is patched; nothing here touches the network.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

try:
    from usage_dashboard.server import cli_home
    from usage_dashboard.server.claude_profile import (
        REFRESH_SKEW_SECONDS,
        ClaudeProfileConfig,
        fetch_claude_profile_usage,
        refresh_command,
    )
except ModuleNotFoundError as exc:  # fcntl: the server runs on Linux only
    pytest.skip(f"requires the POSIX locking backend: {exc}", allow_module_level=True)

from usage_dashboard.server.fetch_types import FetchAuthError, FetchError
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus

_USAGE = "usage_dashboard.server.claude_profile.fetch_claude_usage"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _write_credential(config_dir: Path, access: str, expires_at: int | None) -> None:
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    oauth: dict[str, Any] = {
        "accessToken": access,
        "refreshToken": f"refresh-for-{access}",
        "scopes": ["user:inference", "user:profile"],
        "subscriptionType": "max",
    }
    if expires_at is not None:
        oauth["expiresAt"] = expires_at
    (config_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))


def _access(config_dir: Path) -> str:
    raw = json.loads((config_dir / ".credentials.json").read_text())
    return str(raw["claudeAiOauth"]["accessToken"])


_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "num_turns": 0,
    "total_cost_usd": 0,
    "local_command": "usage",
    "result": "Current session: 10% used",
}


def _stub(
    tmp_path: Path,
    *,
    refresh_to: str | None = "fresh-access",
    only_if_expired: bool = True,
    result: dict[str, Any] | None = None,
    stdout: str | None = None,
    sleep: float = 0.0,
) -> str:
    """A stand-in ``claude`` that logs each run and, like the real CLI,
    rewrites the credential atomically when it decides to refresh."""
    log = tmp_path / "claude-runs.jsonl"
    out = stdout if stdout is not None else json.dumps(result or _RESULT)
    body = f"""
import json, os, sys, time
time.sleep({sleep!r})
cfg = os.environ["CLAUDE_CONFIG_DIR"]
with open({str(log)!r}, "a") as fh:
    fh.write(json.dumps({{
        "argv": sys.argv[1:],
        "env": sorted(os.environ),
        "home": os.environ.get("HOME"),
        "cwd": os.getcwd(),
        "cfg": cfg,
    }}) + "\\n")
path = os.path.join(cfg, ".credentials.json")
refresh_to = {refresh_to!r}
if refresh_to is not None and os.path.exists(path):
    raw = json.load(open(path))
    oauth = raw["claudeAiOauth"]
    expired = oauth.get("expiresAt", 0) <= time.time() * 1000
    if expired or not {only_if_expired!r}:
        oauth["accessToken"] = refresh_to
        oauth["refreshToken"] = "rotated-refresh"
        oauth["expiresAt"] = int(time.time() * 1000) + 8 * 3600 * 1000
        oauth["refreshTokenExpiresAt"] = oauth["expiresAt"] + 1
        tmp = path + ".tmp"
        json.dump(raw, open(tmp, "w"))
        os.replace(tmp, path)
open(os.path.join(os.environ["HOME"], "cache-litter"), "w").write("x")
sys.stdout.write({out!r})
"""
    path = tmp_path / "claude"
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _runs(tmp_path: Path) -> list[dict[str, Any]]:
    log = tmp_path / "claude-runs.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def _reading(provider: Provider = Provider.CLAUDE) -> Reading:
    from datetime import datetime

    return Reading(
        provider=provider,
        status=ReadingStatus.CURRENT,
        session_percent=10.0,
        session_resets_at=None,
        weekly_percent=79.0,
        weekly_resets_at=None,
        fetched_at=datetime(2026, 11, 8, 20, 0, 0),
        stale=False,
    )


def _config(tmp_path: Path, binary: str, timeout: float = 30.0) -> ClaudeProfileConfig:
    return ClaudeProfileConfig(
        config_dir=str(tmp_path / "claude-personal"), binary=binary, timeout=timeout
    )


class TestValidToken:
    def test_a_fresh_token_reads_usage_without_running_the_cli(
        self, tmp_path: Path
    ) -> None:
        cfg = _config(tmp_path, _stub(tmp_path))
        _write_credential(Path(cfg.config_dir), "live-access", _now_ms() + 3600_000)
        with patch(_USAGE, return_value=_reading()) as usage:
            reading = fetch_claude_profile_usage(cfg)
        assert reading.status is ReadingStatus.CURRENT
        usage.assert_called_once_with("live-access", Provider.CLAUDE)
        assert _runs(tmp_path) == []

    def test_the_work_account_passes_its_provider(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, _stub(tmp_path))
        _write_credential(Path(cfg.config_dir), "work-access", _now_ms() + 3600_000)
        with patch(_USAGE, return_value=_reading(Provider.CLAUDE_WORK)) as usage:
            fetch_claude_profile_usage(cfg, Provider.CLAUDE_WORK)
        usage.assert_called_once_with("work-access", Provider.CLAUDE_WORK)


class TestNearExpiry:
    def test_an_expired_token_is_refreshed_by_the_cli_first(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, _stub(tmp_path))
        _write_credential(Path(cfg.config_dir), "stale-access", _now_ms() - 60_000)
        with patch(_USAGE, return_value=_reading()) as usage:
            fetch_claude_profile_usage(cfg)
        usage.assert_called_once_with("fresh-access", Provider.CLAUDE)
        assert len(_runs(tmp_path)) == 1
        # The dashboard never wrote the file: the CLI's rewrite is what landed.
        assert _access(Path(cfg.config_dir)) == "fresh-access"

    def test_inside_the_skew_window_the_trigger_runs(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, _stub(tmp_path, only_if_expired=False))
        soon = _now_ms() + (REFRESH_SKEW_SECONDS - 30) * 1000
        _write_credential(Path(cfg.config_dir), "soon-access", soon)
        with patch(_USAGE, return_value=_reading()) as usage:
            fetch_claude_profile_usage(cfg)
        usage.assert_called_once_with("fresh-access", Provider.CLAUDE)

    def test_a_still_valid_token_the_cli_declines_is_used(self, tmp_path: Path) -> None:
        # The CLI's own buffer may be smaller than ours: it leaves a token that
        # is near (but not past) expiry alone, and that token still works.
        cfg = _config(tmp_path, _stub(tmp_path, only_if_expired=True))
        soon = _now_ms() + (REFRESH_SKEW_SECONDS - 30) * 1000
        _write_credential(Path(cfg.config_dir), "soon-access", soon)
        with patch(_USAGE, return_value=_reading()) as usage:
            fetch_claude_profile_usage(cfg)
        usage.assert_called_once_with("soon-access", Provider.CLAUDE)

    def test_expired_and_not_refreshed_is_an_auth_failure(self, tmp_path: Path) -> None:
        # E.g. a revoked refresh token: the CLI cannot refresh, and says
        # nothing useful (it "succeeds" even when signed out).
        cfg = _config(tmp_path, _stub(tmp_path, refresh_to=None))
        _write_credential(Path(cfg.config_dir), "stale-access", _now_ms() - 60_000)
        with patch(_USAGE) as usage, pytest.raises(FetchAuthError, match="re-enrol"):
            fetch_claude_profile_usage(cfg)
        usage.assert_not_called()


class TestRejection:
    def test_a_401_gets_one_cli_refresh_and_a_retry(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, _stub(tmp_path, only_if_expired=False))
        _write_credential(Path(cfg.config_dir), "revoked-access", _now_ms() + 3600_000)
        with patch(
            _USAGE, side_effect=[FetchAuthError("HTTP 401"), _reading()]
        ) as usage:
            fetch_claude_profile_usage(cfg)
        assert [c.args[0] for c in usage.call_args_list] == [
            "revoked-access",
            "fresh-access",
        ]

    def test_a_401_the_cli_cannot_fix_is_an_auth_failure(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, _stub(tmp_path, refresh_to=None))
        _write_credential(Path(cfg.config_dir), "revoked-access", _now_ms() + 3600_000)
        with (
            patch(_USAGE, side_effect=FetchAuthError("HTTP 401")) as usage,
            pytest.raises(FetchAuthError),
        ):
            fetch_claude_profile_usage(cfg)
        usage.assert_called_once()  # no pointless retry with the same token

    def test_a_transient_failure_does_not_run_the_cli(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, _stub(tmp_path))
        _write_credential(Path(cfg.config_dir), "live-access", _now_ms() + 3600_000)
        with (
            patch(_USAGE, side_effect=FetchError("HTTP 500")),
            pytest.raises(FetchError),
        ):
            fetch_claude_profile_usage(cfg)
        assert _runs(tmp_path) == []


class TestProfileState:
    def test_no_credential_file_is_signed_out(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, _stub(tmp_path))
        with patch(_USAGE) as usage, pytest.raises(FetchAuthError, match="not signed in"):
            fetch_claude_profile_usage(cfg)
        usage.assert_not_called()
        assert _runs(tmp_path) == []

    def test_a_corrupt_file_is_not_an_auth_failure(self, tmp_path: Path) -> None:
        # Schema drift or a broken file is not fixed by a re-login, so it must
        # not enter the re-auth path (and never echoes file content).
        cfg = _config(tmp_path, _stub(tmp_path))
        Path(cfg.config_dir).mkdir(parents=True)
        (Path(cfg.config_dir) / ".credentials.json").write_text("{not json")
        with pytest.raises(FetchError) as exc:
            fetch_claude_profile_usage(cfg)
        assert not isinstance(exc.value, FetchAuthError)

    def test_an_unknown_schema_is_not_an_auth_failure(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, _stub(tmp_path))
        Path(cfg.config_dir).mkdir(parents=True)
        (Path(cfg.config_dir) / ".credentials.json").write_text('{"other": {}}')
        with pytest.raises(FetchError, match="Unrecognised") as exc:
            fetch_claude_profile_usage(cfg)
        assert not isinstance(exc.value, FetchAuthError)

    def test_an_interrupted_swap_is_settled_before_reading(self, tmp_path: Path) -> None:
        # Died between the two renames of an enrolment swap: the old login is
        # the only one, and the next fetch restores it.
        cfg = _config(tmp_path, _stub(tmp_path))
        prev = Path(f"{cfg.config_dir}.prev")
        _write_credential(prev, "old-access", _now_ms() + 3600_000)
        Path(f"{cfg.config_dir}.swapping").write_text("")
        with patch(_USAGE, return_value=_reading()) as usage:
            fetch_claude_profile_usage(cfg)
        usage.assert_called_once_with("old-access", Provider.CLAUDE)


class TestTheTriggerRun:
    def _run_once(self, tmp_path: Path, **stub: Any) -> ClaudeProfileConfig:
        cfg = _config(tmp_path, _stub(tmp_path, **stub))
        _write_credential(Path(cfg.config_dir), "stale-access", _now_ms() - 60_000)
        with patch(_USAGE, return_value=_reading()):
            fetch_claude_profile_usage(cfg)
        return cfg

    def test_argv_is_the_verified_zero_turn_command(self, tmp_path: Path) -> None:
        self._run_once(tmp_path)
        (run,) = _runs(tmp_path)
        assert run["argv"] == refresh_command("claude")[1:]
        assert "--no-session-persistence" in run["argv"]
        assert "--bare" not in run["argv"]  # --bare disables OAuth entirely

    def test_the_child_gets_a_minimal_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("API_KEY", "dashboard-secret")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "would-switch-auth-mode")
        monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
        cfg = self._run_once(tmp_path)
        (run,) = _runs(tmp_path)
        assert "API_KEY" not in run["env"]
        assert "ANTHROPIC_API_KEY" not in run["env"]
        assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" not in run["env"]
        assert "DISABLE_AUTOUPDATER" in run["env"]
        assert run["cfg"] == cfg.config_dir

    def test_home_is_a_throwaway_directory(self, tmp_path: Path) -> None:
        cfg = self._run_once(tmp_path)
        (run,) = _runs(tmp_path)
        assert run["home"] != cfg.config_dir
        assert run["cwd"] == run["home"]
        # The per-run cache the CLI writes under HOME does not accumulate.
        assert not os.path.exists(run["home"])

    def test_a_model_turn_is_refused(self, tmp_path: Path) -> None:
        # A setting that turned /usage into a prompt would cost quota on
        # every refresh; refuse it loudly instead.
        cfg = _config(tmp_path, _stub(tmp_path, result=dict(_RESULT, num_turns=1)))
        _write_credential(Path(cfg.config_dir), "stale-access", _now_ms() - 60_000)
        with pytest.raises(FetchError, match="model turn"):
            fetch_claude_profile_usage(cfg)

    def test_non_json_output_is_an_error(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, _stub(tmp_path, stdout="Please run /login"))
        _write_credential(Path(cfg.config_dir), "stale-access", _now_ms() - 60_000)
        with pytest.raises(FetchError, match="without a JSON result"):
            fetch_claude_profile_usage(cfg)

    def test_a_wedged_cli_is_killed(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, _stub(tmp_path, sleep=30), timeout=0.5)
        _write_credential(Path(cfg.config_dir), "stale-access", _now_ms() - 60_000)
        started = time.monotonic()
        with pytest.raises(FetchError, match="timed out"):
            fetch_claude_profile_usage(cfg)
        assert time.monotonic() - started < 10

    def test_a_missing_binary_is_an_error(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, str(tmp_path / "no-such-claude"))
        _write_credential(Path(cfg.config_dir), "stale-access", _now_ms() - 60_000)
        with pytest.raises(FetchError, match="could not start"):
            fetch_claude_profile_usage(cfg)


class TestOneOwner:
    def test_fetch_and_swap_exclude_each_other(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, _stub(tmp_path))
        _write_credential(Path(cfg.config_dir), "live-access", _now_ms() + 3600_000)
        released = threading.Event()
        holding = threading.Event()

        def hold() -> None:
            with cli_home.home_lock(cfg.config_dir):
                holding.set()
                released.wait(10)

        holder = threading.Thread(target=hold)
        holder.start()
        holding.wait(5)
        done = threading.Event()

        def fetch() -> None:
            with patch(_USAGE, return_value=_reading()):
                fetch_claude_profile_usage(cfg)
            done.set()

        threading.Thread(target=fetch, daemon=True).start()
        assert not done.wait(0.5), "fetch ran while the profile lock was held"
        released.set()
        holder.join(5)
        assert done.wait(10), "fetch never ran after the lock was released"


def _raw_stub(tmp_path: Path, body: str) -> str:
    path = tmp_path / "claude"
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class TestFailureClasses:
    """Review findings 4/5: an auth failure parks the tile behind "login
    required" at the backoff cap, so transient trigger trouble must not be one,
    and a token that has not expired yet must still be used."""

    def test_cli_reported_auth_error_is_an_auth_failure(self, tmp_path: Path) -> None:
        cfg = _config(
            tmp_path,
            _stub(
                tmp_path,
                refresh_to=None,
                result=dict(
                    _RESULT, is_error=True, result="OAuth token expired. Please run /login"
                ),
            ),
        )
        _write_credential(Path(cfg.config_dir), "stale-access", _now_ms() - 60_000)
        with pytest.raises(FetchAuthError):
            fetch_claude_profile_usage(cfg)

    def test_cli_reported_other_error_is_transient(self, tmp_path: Path) -> None:
        cfg = _config(
            tmp_path,
            _stub(
                tmp_path,
                refresh_to=None,
                result=dict(_RESULT, is_error=True, result="API Error: 529 Overloaded"),
            ),
        )
        _write_credential(Path(cfg.config_dir), "stale-access", _now_ms() - 60_000)
        with pytest.raises(FetchError) as exc:
            fetch_claude_profile_usage(cfg)
        assert not isinstance(exc.value, FetchAuthError)

    def test_failed_trigger_with_expired_token_is_transient(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, _stub(tmp_path, stdout="garbage"))
        _write_credential(Path(cfg.config_dir), "stale-access", _now_ms() - 60_000)
        with pytest.raises(FetchError) as exc:
            fetch_claude_profile_usage(cfg)
        assert not isinstance(exc.value, FetchAuthError)

    def test_failed_trigger_keeps_a_still_valid_token(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, _stub(tmp_path, stdout="garbage"))
        soon = _now_ms() + (REFRESH_SKEW_SECONDS - 30) * 1000
        _write_credential(Path(cfg.config_dir), "soon-access", soon)
        with patch(_USAGE, return_value=_reading()) as usage:
            fetch_claude_profile_usage(cfg)
        usage.assert_called_once_with("soon-access", Provider.CLAUDE)

    def test_a_401_with_a_failed_trigger_is_transient(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, _stub(tmp_path, stdout="garbage"))
        _write_credential(Path(cfg.config_dir), "live-access", _now_ms() + 3600_000)
        with (
            patch(_USAGE, side_effect=FetchAuthError("HTTP 401")),
            pytest.raises(FetchError) as exc,
        ):
            fetch_claude_profile_usage(cfg)
        assert not isinstance(exc.value, FetchAuthError)


class TestStoppingAWedgedCli:
    def test_sigterm_comes_first_so_the_cli_can_finish_its_write(
        self, tmp_path: Path
    ) -> None:
        marker = tmp_path / "got-sigterm"
        binary = _raw_stub(
            tmp_path,
            f"""
import signal, sys, time
def on_term(signum, frame):
    open({str(marker)!r}, "w").write("term")
    sys.exit(0)
signal.signal(signal.SIGTERM, on_term)
time.sleep(60)
""",
        )
        cfg = _config(tmp_path, binary, timeout=0.5)
        _write_credential(Path(cfg.config_dir), "stale-access", _now_ms() - 60_000)
        with pytest.raises(FetchError, match="timed out"):
            fetch_claude_profile_usage(cfg)
        assert marker.read_text() == "term"

    def test_the_whole_process_group_is_stopped(self, tmp_path: Path) -> None:
        pidfile = tmp_path / "grandchild.pid"
        binary = _raw_stub(
            tmp_path,
            f"""
import signal, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open({str(pidfile)!r}, "w").write(str(child.pid))
signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a leader that ignores TERM
time.sleep(60)
""",
        )
        cfg = _config(tmp_path, binary, timeout=0.5)
        _write_credential(Path(cfg.config_dir), "stale-access", _now_ms() - 60_000)
        with pytest.raises(FetchError, match="timed out"):
            fetch_claude_profile_usage(cfg)
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            # Reaped by init or still a zombie: a zombie reads as alive here.
            try:
                with open(f"/proc/{pid}/status") as status:
                    if "State:\tZ" in status.read():
                        return
            except FileNotFoundError:
                return
            time.sleep(0.05)
        raise AssertionError("grandchild survived the timeout")


class TestOneOwnerAcrossProcesses:
    def test_enrolment_swap_in_another_process_excludes_a_fetch(
        self, tmp_path: Path
    ) -> None:
        # `kubectl exec usage-dashboard login claude` is a second process:
        # the swap must take the same lock the fetcher holds.
        import subprocess

        cfg = _config(tmp_path, _stub(tmp_path))
        _write_credential(Path(cfg.config_dir), "old-access", _now_ms() + 3600_000)
        staging = Path(cli_home.make_staging_home(cfg.config_dir))
        _write_credential(staging, "new-access", _now_ms() + 3600_000)
        ready = tmp_path / "holding"
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import pathlib, sys, time\n"
                "from usage_dashboard.server import cli_home\n"
                "with cli_home.home_lock(sys.argv[1]):\n"
                "    pathlib.Path(sys.argv[3]).write_text('x')\n"
                "    time.sleep(1.0)\n"
                "cli_home.install_home(sys.argv[2], sys.argv[1])\n",
                cfg.config_dir,
                str(staging),
                str(ready),
            ]
        )
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert time.monotonic() < deadline, "holder never took the lock"
            time.sleep(0.02)
        started = time.monotonic()
        with patch(_USAGE, return_value=_reading()) as usage:
            fetch_claude_profile_usage(cfg)
        assert time.monotonic() - started >= 0.5, "fetch did not wait for the lock"
        holder.wait(10)
        # Whichever ran first, the fetch saw a whole profile, never a half swap.
        assert usage.call_args.args[0] in ("old-access", "new-access")
        assert _access(Path(cfg.config_dir)) == "new-access"
