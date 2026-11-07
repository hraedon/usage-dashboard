"""Plan 007 — Google Antigravity via the official ``agy`` CLI.

Covers the ``/usage`` parser (against a live 1.4.12 capture), the subprocess
contract (argv, minimal env, signed-out fast-fail, timeout), the staging-home
swap, and the ``login antigravity`` ceremony's verify-then-swap guarantee.
The real binary is never run here: stub ``agy`` scripts stand in for it, so
anything they emit mirrors the behaviour verified against 1.4.12.
"""
from __future__ import annotations

import copy
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from usage_dashboard import cli
from usage_dashboard.server import fetch_antigravity as agy
from usage_dashboard.server.fetch_antigravity import (
    AntigravityConfig,
    fetch_antigravity_usage,
    install_home,
    make_staging_home,
    parse_usage,
    run_usage_command,
)
from usage_dashboard.server.fetch_types import FetchAuthError, FetchError
from usage_dashboard.shared.models import Provider

_FIXTURE = Path(__file__).parent / "fixtures" / "antigravity_usage_1.4.12.json"
_TOKEN_REL = Path(".gemini/antigravity-cli/antigravity-oauth-token")


def _payload() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _groups(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return payload["command"]["data"]["groups"]


def _stub(tmp_path: Path, body: str, name: str = "agy") -> str:
    """An executable stand-in for ``agy``. The fetcher hands the child a
    minimal environment, so behaviour is baked into the script itself."""
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _success_stub(tmp_path: Path, payload: dict[str, Any] | None = None) -> str:
    """Prints a /usage result and, like a completed sign-in, leaves a token."""
    blob = json.dumps(payload if payload is not None else _payload())
    return _stub(
        tmp_path,
        f"""
import os, pathlib, sys
tok = pathlib.Path(os.environ["HOME"]) / {str(_TOKEN_REL)!r}
tok.parent.mkdir(parents=True, exist_ok=True)
tok.write_text("signed-in")
sys.stdout.write({blob!r})
""",
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestParseUsage:
    def test_live_fixture(self) -> None:
        reading = parse_usage(_payload())
        assert reading.provider is Provider.ANTIGRAVITY
        # gemini-5h is untouched (remaining 1): 0% used, and its sliding
        # "now + 5h" reset is dropped rather than reported.
        assert reading.session_percent == 0.0
        assert reading.session_resets_at is None
        assert reading.weekly_percent == pytest.approx(0.07, abs=0.01)
        assert reading.weekly_resets_at == datetime(2026, 11, 5, 2, 46, 19)
        assert reading.scoped_limits is not None
        (argon,) = reading.scoped_limits
        assert argon.name == "Argon"
        assert argon.percent == 100.0  # remaining_fraction 0 = exhausted
        assert argon.resets_at == datetime(2026, 11, 4, 15, 18, 24)

    def test_claude_and_gpt_bucket_is_ignored(self) -> None:
        # 3p-weekly is 85% remaining (15.3% used) in the fixture; no field
        # may carry it.
        reading = parse_usage(_payload())
        values = [reading.session_percent, reading.weekly_percent] + [
            s.percent for s in reading.scoped_limits or []
        ]
        assert all(v is None or abs(v - 15.32) > 1 for v in values)

    def test_disabled_bucket_reads_exhausted(self) -> None:
        payload = _payload()
        gemini = _groups(payload)[0]["buckets"]
        gemini[0]["disabled"] = True  # gemini-weekly, still reports ~1.0
        assert parse_usage(payload).weekly_percent == 100.0

    def test_missing_fraction_with_reset_reads_exhausted(self) -> None:
        # Proto3 JSON drops a zero remaining_fraction.
        payload = _payload()
        argon_bucket = _groups(payload)[2]["buckets"][0]
        del argon_bucket["remaining_fraction"]
        (argon,) = parse_usage(payload).scoped_limits or []
        assert argon.percent == 100.0

    def test_exact_pool_ids_beat_a_new_gemini_group_listed_first(self) -> None:
        payload = _payload()
        _groups(payload).insert(0, {
            "name": "Gemini 3 Deep Think",
            "buckets": [
                {"id": "deepthink-5h", "window": "5h", "remaining_fraction": 0.1,
                 "reset_time": "2026-11-01T09:00:00Z"},
                {"id": "deepthink-weekly", "window": "weekly", "remaining_fraction": 0.2,
                 "reset_time": "2026-11-06T09:00:00Z"},
            ],
        })
        reading = parse_usage(payload)
        assert reading.session_percent == 0.0  # gemini-5h, not deepthink-5h
        assert reading.weekly_percent == pytest.approx(0.07, abs=0.01)

    def test_ultra_shape_without_argon_group(self) -> None:
        payload = _payload()
        _groups(payload).pop(2)
        reading = parse_usage(payload)
        assert reading.scoped_limits is None
        assert reading.weekly_percent is not None

    def test_matches_argon_by_group_name_when_the_id_shifts(self) -> None:
        payload = _payload()
        _groups(payload)[2]["buckets"][0]["id"] = "argon-weekly-v2"
        (argon,) = parse_usage(payload).scoped_limits or []
        assert argon.percent == 100.0

    def test_no_gemini_buckets_is_loud(self) -> None:
        payload = _payload()
        payload["command"]["data"]["groups"] = [_groups(payload)[1]]  # 3p only
        with pytest.raises(FetchError, match="no Gemini quota buckets"):
            parse_usage(payload)

    def test_signed_out_result_is_an_auth_failure(self) -> None:
        payload = {
            "status": "ERROR",
            "error": "authentication failed or timed out",
            "num_turns": 0,
        }
        with pytest.raises(FetchAuthError):
            parse_usage(payload)

    def test_other_error_result_is_a_fetch_error(self) -> None:
        with pytest.raises(FetchError, match="ERROR"):
            parse_usage({"status": "ERROR", "error": "backend unavailable"})

    def test_a_result_that_spent_a_turn_is_refused(self) -> None:
        # The disableSlashCommands bug: /usage went to the model.
        payload = _payload()
        payload["num_turns"] = 1
        with pytest.raises(FetchError, match="model turn"):
            parse_usage(payload)

    def test_a_non_usage_answer_is_refused(self) -> None:
        payload = _payload()
        payload["command"]["name"] = "model"
        with pytest.raises(FetchError, match="not a /usage answer"):
            parse_usage(payload)

    def test_fraction_is_clamped(self) -> None:
        payload = _payload()
        _groups(payload)[0]["buckets"][0]["remaining_fraction"] = 1.7
        _groups(payload)[2]["buckets"][0]["remaining_fraction"] = -0.2
        reading = parse_usage(payload)
        assert reading.weekly_percent == 0.0
        assert (reading.scoped_limits or [])[0].percent == 100.0


# ---------------------------------------------------------------------------
# The subprocess contract
# ---------------------------------------------------------------------------


class TestRunUsageCommand:
    def test_argv_and_minimal_env(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("API_KEY", "dashboard-secret")
        monkeypatch.setenv("GEMINI_API_KEY", "would-switch-auth-mode")
        record = tmp_path / "record.json"
        binary = _stub(
            tmp_path,
            f"""
import json, os, sys
json.dump({{"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(),
           "stdin_tty": os.isatty(0), "stdin": sys.stdin.read()}},
          open({str(record)!r}, "w"))
sys.stdout.write({json.dumps(_payload())!r})
""",
        )
        home = tmp_path / "home"
        reading = fetch_antigravity_usage(AntigravityConfig(binary=binary, home=str(home)))
        assert reading.provider is Provider.ANTIGRAVITY

        seen = json.loads(record.read_text())
        assert seen["argv"] == [
            "-p", "/usage", "--output-format", "json",
            "--disable-slash-commands=false", "--log-file", os.devnull,
        ]
        env = seen["env"]
        assert env["HOME"] == str(home)
        assert env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"  # "1" is ignored
        assert "API_KEY" not in env
        assert "GEMINI_API_KEY" not in env
        assert set(env) <= set(agy._ENV_PASSTHROUGH) | {
            "HOME", "AGY_CLI_DISABLE_AUTO_UPDATE",
        }
        assert seen["cwd"] == str(home / "workspace")
        assert seen["stdin"] == ""  # closed, never a terminal to block on
        assert not seen["stdin_tty"]

    def test_signed_out_fails_fast_instead_of_waiting_out_the_window(
        self, tmp_path: Path
    ) -> None:
        binary = _stub(
            tmp_path,
            """
import sys, time
sys.stderr.write("Authentication required. Please visit the URL to log in:\\n")
sys.stderr.write("  https://accounts.google.com/o/oauth2/auth?client_id=x\\n")
sys.stderr.write("Waiting for authentication (timeout 60s)...\\n")
sys.stderr.flush()
time.sleep(60)
""",
        )
        started = time.monotonic()
        with pytest.raises(FetchAuthError):
            run_usage_command(
                AntigravityConfig(binary=binary, home=str(tmp_path / "h"), timeout=30)
            )
        assert time.monotonic() - started < 10

    def test_timeout_kills_the_child(self, tmp_path: Path) -> None:
        pidfile = tmp_path / "pid"
        binary = _stub(
            tmp_path,
            f"""
import os, time
open({str(pidfile)!r}, "w").write(str(os.getpid()))
time.sleep(60)
""",
        )
        with pytest.raises(FetchError, match="timed out"):
            run_usage_command(
                AntigravityConfig(binary=binary, home=str(tmp_path / "h"), timeout=1.0)
            )
        pid = int(pidfile.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    def test_timeout_kills_grandchildren_too(self, tmp_path: Path) -> None:
        # agy forks (xdg-open, verified); a grandchild holding stdout open
        # must die with it, or the poll would wait on the pipe.
        pidfile = tmp_path / "grandchild.pid"
        binary = _stub(
            tmp_path,
            f"""
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open({str(pidfile)!r}, "w").write(str(child.pid))
time.sleep(60)
""",
        )
        started = time.monotonic()
        with pytest.raises(FetchError, match="timed out"):
            run_usage_command(
                AntigravityConfig(binary=binary, home=str(tmp_path / "h"), timeout=1.0)
            )
        assert time.monotonic() - started < 10
        grandchild = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            # Reparented to this test process's init; a zombie still answers
            # kill(0), so also accept the zombie state.
            try:
                with open(f"/proc/{grandchild}/stat") as fh:
                    if fh.read().split()[2] == "Z":
                        break
            except OSError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("grandchild outlived the timed-out fetch")

    def test_result_after_chatter_is_still_parsed(self, tmp_path: Path) -> None:
        binary = _stub(
            tmp_path,
            "print('Signed in as someone.')\nprint()\n"
            f"print({json.dumps(json.dumps(_payload()))})\n",
        )
        payload = run_usage_command(AntigravityConfig(binary=binary, home=str(tmp_path / "h")))
        assert payload["status"] == "SUCCESS"

    def test_non_json_output(self, tmp_path: Path) -> None:
        binary = _stub(tmp_path, "print('Gemini Models\\tWeekly\\t100%')\n")
        with pytest.raises(FetchError, match="not JSON"):
            run_usage_command(AntigravityConfig(binary=binary, home=str(tmp_path / "h")))

    def test_missing_binary(self, tmp_path: Path) -> None:
        with pytest.raises(FetchError, match="could not start"):
            run_usage_command(
                AntigravityConfig(binary=str(tmp_path / "nope"), home=str(tmp_path / "h"))
            )


# ---------------------------------------------------------------------------
# One owner of the home directory
# ---------------------------------------------------------------------------


class TestHomeSwap:
    def test_staging_is_a_private_sibling(self, tmp_path: Path) -> None:
        home = tmp_path / "antigravity"
        staging = Path(make_staging_home(str(home)))
        assert staging.parent == tmp_path
        assert staging.name.startswith("antigravity.enrol-")
        assert stat.S_IMODE(staging.stat().st_mode) == 0o700

    def test_install_replaces_the_previous_home(self, tmp_path: Path) -> None:
        home = tmp_path / "antigravity"
        home.mkdir()
        (home / "old").write_text("old login")
        staging = Path(make_staging_home(str(home)))
        (staging / "new").write_text("new login")

        install_home(str(staging), str(home))

        assert (home / "new").read_text() == "new login"
        assert not (home / "old").exists()
        assert not staging.exists()
        assert not (tmp_path / "antigravity.prev").exists()

    def test_install_into_an_empty_slot(self, tmp_path: Path) -> None:
        home = tmp_path / "antigravity"
        staging = Path(make_staging_home(str(home)))
        (staging / "new").write_text("x")
        install_home(str(staging), str(home))
        assert (home / "new").exists()

    def test_a_fetch_undoes_an_interrupted_swap(self, tmp_path: Path) -> None:
        # Died between "home -> .prev" and "staging -> home": the next fetch
        # must find the previous login, not a blank home that reads as
        # signed out.
        home = tmp_path / "antigravity"
        prev = tmp_path / "antigravity.prev"
        prev.mkdir()
        (prev / "kept").write_text("login")
        (tmp_path / "antigravity.swapping").write_text("")
        binary = _success_stub(tmp_path)
        fetch_antigravity_usage(AntigravityConfig(binary=binary, home=str(home)))
        assert (home / "kept").read_text() == "login"
        assert not prev.exists()
        assert not (tmp_path / "antigravity.swapping").exists()

    def test_deleting_the_home_signs_out_for_good(self, tmp_path: Path) -> None:
        # A leftover .prev (swap died after its renames) must not resurrect
        # the old login once the operator deletes the home to sign out.
        home = tmp_path / "antigravity"
        prev = tmp_path / "antigravity.prev"
        prev.mkdir()
        (prev / "old").write_text("old login")
        binary = _success_stub(tmp_path)
        fetch_antigravity_usage(AntigravityConfig(binary=binary, home=str(home)))
        assert not (home / "old").exists()
        assert not prev.exists()

    def test_fetch_and_swap_exclude_each_other(self, tmp_path: Path) -> None:
        home = tmp_path / "antigravity"
        binary = _success_stub(tmp_path)
        released = threading.Event()
        holding = threading.Event()

        def hold() -> None:
            with agy.home_lock(str(home)):
                holding.set()
                released.wait(10)

        holder = threading.Thread(target=hold)
        holder.start()
        holding.wait(5)
        done = threading.Event()
        threading.Thread(
            target=lambda: (
                fetch_antigravity_usage(AntigravityConfig(binary=binary, home=str(home))),
                done.set(),
            ),
            daemon=True,
        ).start()
        assert not done.wait(0.5), "fetch ran while the home lock was held"
        released.set()
        holder.join(5)
        assert done.wait(10), "fetch never ran after the lock was released"

    def test_stale_staging_dirs_are_swept(self, tmp_path: Path) -> None:
        home = tmp_path / "antigravity"
        stale = tmp_path / "antigravity.enrol-dead"
        stale.mkdir()
        old = time.time() - 2 * agy._STALE_STAGING_SECONDS
        os.utime(stale, (old, old))
        fresh = tmp_path / "antigravity.enrol-live"
        fresh.mkdir()
        make_staging_home(str(home))
        assert not stale.exists()
        assert fresh.exists()


# ---------------------------------------------------------------------------
# `usage-dashboard login antigravity`
# ---------------------------------------------------------------------------


def _leftover_staging(tmp_path: Path) -> list[Path]:
    return sorted(tmp_path.glob("antigravity.enrol-*"))


@pytest.fixture()
def existing_home(tmp_path: Path) -> Path:
    home = tmp_path / "antigravity"
    (home / _TOKEN_REL).parent.mkdir(parents=True)
    (home / _TOKEN_REL).write_text("working login")
    return home


class TestLoginCeremony:
    def test_sigterm_handler_is_restored(self, tmp_path: Path, existing_home: Path) -> None:
        before = signal.getsignal(signal.SIGTERM)
        cli.login_antigravity(home=str(existing_home), agy_bin=_success_stub(tmp_path))
        assert signal.getsignal(signal.SIGTERM) is before
        with pytest.raises(SystemExit):
            cli.login_antigravity(
                home=str(existing_home), agy_bin=_stub(tmp_path, "print('x')\n", "bad")
            )
        assert signal.getsignal(signal.SIGTERM) is before

    def test_success_swaps_in_the_verified_login(
        self, tmp_path: Path, existing_home: Path, capsys
    ) -> None:
        cli.login_antigravity(home=str(existing_home), agy_bin=_success_stub(tmp_path))
        assert (existing_home / _TOKEN_REL).read_text() == "signed-in"
        assert _leftover_staging(tmp_path) == []
        out = capsys.readouterr().out
        assert "Gemini 4 Argon weekly: 100% used" in out

    @pytest.mark.parametrize(
        "body",
        [
            # The CLI's own 60 s window ran out.
            'print(\'{"status":"ERROR","error":"authentication failed or timed out",'
            '"num_turns":0}\')\nraise SystemExit(1)\n',
            # Signed in, but the result spent a model turn.
            "import json\n"
            f"p = json.loads({json.dumps(json.dumps(_payload()))})\n"
            "p['num_turns'] = 2\nprint(json.dumps(p))\n",
            # Garbage instead of a result.
            "print('not json')\n",
        ],
        ids=["timed-out", "spent-a-turn", "garbage"],
    )
    def test_failure_never_displaces_a_working_login(
        self, tmp_path: Path, existing_home: Path, body: str
    ) -> None:
        binary = _stub(
            tmp_path,
            "import os, pathlib\n"
            f"t = pathlib.Path(os.environ['HOME']) / {str(_TOKEN_REL)!r}\n"
            "t.parent.mkdir(parents=True, exist_ok=True)\n"
            "t.write_text('half-finished')\n" + body,
        )
        with pytest.raises(SystemExit) as exc:
            cli.login_antigravity(home=str(existing_home), agy_bin=binary)
        assert exc.value.code == 1
        assert (existing_home / _TOKEN_REL).read_text() == "working login"
        assert _leftover_staging(tmp_path) == []

    def test_sigterm_cleans_up_staging_and_the_child(
        self, tmp_path: Path, existing_home: Path
    ) -> None:
        """The login pane cancels with SIGTERM; Python's default action would
        skip ``finally`` and strand both the staging dir and the agy child."""
        pidfile = tmp_path / "agy.pid"
        binary = _stub(
            tmp_path,
            f"""
import os, time
open({str(pidfile)!r}, "w").write(str(os.getpid()))
time.sleep(60)
""",
        )
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "usage_dashboard.cli", "login", "antigravity",
                "--antigravity-home", str(existing_home), "--agy-bin", binary,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 15
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pidfile.exists(), "stub agy never started"
        assert _leftover_staging(tmp_path), "ceremony should be mid-flight"

        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=15) == 128 + signal.SIGTERM

        assert _leftover_staging(tmp_path) == []
        assert (existing_home / _TOKEN_REL).read_text() == "working login"
        child = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("agy child outlived the cancelled ceremony")


def test_fixture_carries_no_identity_fields() -> None:
    """The fixture is a live capture from the owner's account; keep it that
    way (this repo is public)."""
    text = _FIXTURE.read_text(encoding="utf-8")
    assert "@" not in text
    blob = copy.deepcopy(_payload())
    keys: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            keys.update(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(blob)
    assert not keys & {"email", "account", "account_id", "user", "name_email"}
