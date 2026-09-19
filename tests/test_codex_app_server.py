"""Protocol-level tests for the Codex App Server client.

These drive a *fake* `codex` executable, so CI needs no real account, no
network and no Codex install. The scenarios mirror behaviour observed from the
real binary (codex-cli 0.155.1): interleaved notifications, out-of-order
responses, and a JSON-RPC error when no account is enrolled.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from usage_dashboard.server.codex_app_server import (
    DEFAULT_CODEX_ARGS,
    AppServerSession,
    CodexAppServerClient,
    CodexAppServerConfig,
    CodexLoginRequired,
)
from usage_dashboard.server.fetch_types import FetchError

# A live account's rate-limit result, trimmed.
_RATE_LIMITS = {
    "rateLimitsByLimitId": {
        "codex": {
            "limitId": "codex",
            "primary": {"usedPercent": 93, "windowDurationMins": 10080,
                        "resetsAt": 1789832809},
            "secondary": None,
        }
    },
    "rateLimits": {"limitId": "codex", "primary": None, "secondary": None},
}
_ACCOUNT = {"account": {"type": "chatgpt", "planType": "pro"}, "requiresOpenaiAuth": True}


def _fake_codex(tmp_path: Path, behaviour: str) -> Path:
    """Write a fake `codex` executable that plays *behaviour*."""
    script = tmp_path / "codex"
    script.write_text(
        textwrap.dedent(
            f'''\
            #!{sys.executable}
            import json, os, sys, time

            BEHAVIOUR = {behaviour!r}
            ACCOUNT = json.loads({json.dumps(json.dumps(_ACCOUNT))})
            RATE_LIMITS = json.loads({json.dumps(json.dumps(_RATE_LIMITS))})

            def out(obj):
                sys.stdout.write(json.dumps(obj) + "\\n")
                sys.stdout.flush()

            # Record the argv/env the client launched us with, for assertions.
            with open(os.path.join(os.environ["CODEX_HOME"], "invocation.json"), "w") as fh:
                json.dump({{"argv": sys.argv[1:],
                           "codex_home": os.environ.get("CODEX_HOME")}}, fh)

            if BEHAVIOUR == "immediate_exit":
                sys.exit(3)
            if BEHAVIOUR == "stderr_flood":
                # Enough to fill a pipe buffer if nobody is draining it.
                for i in range(20000):
                    sys.stderr.write("noise %d ................................\\n" % i)
                sys.stderr.flush()

            pending = []
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                msg = json.loads(line)
                method, rid = msg.get("method"), msg.get("id")

                if BEHAVIOUR == "garbage_then_ok" and method == "initialize":
                    sys.stdout.write("this is not json\\n")
                    sys.stdout.flush()

                if rid is None:
                    continue  # a notification to us

                if method == "initialize":
                    out({{"id": rid, "result": {{"userAgent": "fake",
                          "codexHome": os.environ.get("CODEX_HOME")}}}})
                elif method == "account/read":
                    if BEHAVIOUR == "logged_out":
                        out({{"id": rid, "result": {{"account": None,
                              "requiresOpenaiAuth": True}}}})
                    elif BEHAVIOUR == "reorder":
                        # Deliver a response for a DIFFERENT id first. The real
                        # binary was seen answering id=3 before id=2, so a
                        # reader that trusts arrival order returns the wrong
                        # payload here.
                        out({{"id": 999, "result": {{"account": {{"type": "WRONG"}}}}}})
                        out({{"method": "remoteControl/status/changed", "params": {{}}}})
                        out({{"id": rid, "result": ACCOUNT}})
                    else:
                        out({{"method": "remoteControl/status/changed",
                              "params": {{"state": "idle"}}}})
                        out({{"id": rid, "result": ACCOUNT}})
                elif method == "account/rateLimits/read":
                    if BEHAVIOUR == "logged_out":
                        out({{"id": rid, "error": {{"code": -32600, "message":
                             "codex account authentication required to read rate limits"}}}})
                    else:
                        out({{"id": rid, "result": RATE_LIMITS}})
                elif method == "account/login/start":
                    out({{"id": rid, "result": {{"type": "chatgptDeviceCode",
                          "loginId": "login-1",
                          "verificationUrl": "https://auth.openai.com/codex/device",
                          "userCode": "L1G2-RW770"}}}})
                    if BEHAVIOUR != "login_never_completes":
                        out({{"method": "account/login/completed",
                              "params": {{"loginId": "login-1"}}}})
                elif method == "account/login/cancel":
                    with open(os.path.join(os.environ["CODEX_HOME"], "cancelled"), "w") as fh:
                        fh.write(json.dumps(msg.get("params")))
                    out({{"id": rid, "result": {{"status": "canceled"}}}})
                elif method == "hang":
                    time.sleep(30)
                else:
                    out({{"id": rid, "error": {{"code": -32601,
                          "message": "unknown method"}}}})
            '''
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _config(tmp_path: Path, behaviour: str = "ok", **kw: Any) -> CodexAppServerConfig:
    home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    return CodexAppServerConfig(
        binary=str(_fake_codex(tmp_path, behaviour)),
        home=str(home),
        args=DEFAULT_CODEX_ARGS,
        **kw,
    )


class TestLaunch:
    def test_launches_with_the_sandbox_flags_and_codex_home(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        client = CodexAppServerClient(config)
        try:
            client.read_account()
        finally:
            client.close()
        invocation = json.loads(
            (Path(config.home) / "invocation.json").read_text()
        )
        # Flag order matters: these are CLI globals, so `app-server` is last.
        assert invocation["argv"] == list(DEFAULT_CODEX_ARGS)
        assert invocation["argv"][-1] == "app-server"
        # Plugins off: the App Server otherwise downloads ~50 MB of catalog and
        # Office templates into CODEX_HOME that this client can never reach.
        assert "--disable" in invocation["argv"]
        for feature in ("plugins", "remote_plugin", "plugin_sharing"):
            assert feature in invocation["argv"]
        assert invocation["codex_home"] == config.home

    def test_missing_binary_raises_fetch_error(self, tmp_path: Path) -> None:
        config = CodexAppServerConfig(
            binary=str(tmp_path / "nope"), home=str(tmp_path / "h")
        )
        client = CodexAppServerClient(config)
        with pytest.raises(FetchError):
            client.read_account()
        client.close()


class TestProtocol:
    def test_reads_account_and_rate_limits(self, tmp_path: Path) -> None:
        client = CodexAppServerClient(_config(tmp_path))
        try:
            account, limits = client.read_account_and_rate_limits()
        finally:
            client.close()
        assert account["account"]["type"] == "chatgpt"
        assert limits["rateLimitsByLimitId"]["codex"]["primary"]["usedPercent"] == 93

    def test_interleaved_notifications_do_not_break_correlation(
        self, tmp_path: Path
    ) -> None:
        # The fake emits remoteControl/status/changed before every
        # account/read response, as the real binary does.
        client = CodexAppServerClient(_config(tmp_path))
        try:
            assert client.read_account()["account"]["planType"] == "pro"
        finally:
            client.close()

    def test_out_of_order_responses_are_correlated_by_id(self, tmp_path: Path) -> None:
        # The real binary answered id=3 before id=2. Reading "the next line"
        # would hand the rate-limit payload back as the account result.
        client = CodexAppServerClient(_config(tmp_path, "reorder"))
        try:
            account, limits = client.read_account_and_rate_limits()
        finally:
            client.close()
        assert account["account"]["type"] == "chatgpt"
        assert "rateLimitsByLimitId" in limits

    def test_non_json_lines_are_tolerated(self, tmp_path: Path) -> None:
        client = CodexAppServerClient(_config(tmp_path, "garbage_then_ok"))
        try:
            assert client.read_account()["account"]["type"] == "chatgpt"
        finally:
            client.close()

    def test_logged_out_raises_login_required(self, tmp_path: Path) -> None:
        client = CodexAppServerClient(_config(tmp_path, "logged_out"))
        try:
            with pytest.raises(CodexLoginRequired):
                client.read_account_and_rate_limits()
        finally:
            client.close()

    def test_rate_limits_error_maps_to_login_required(self, tmp_path: Path) -> None:
        client = CodexAppServerClient(_config(tmp_path, "logged_out"))
        try:
            with pytest.raises(CodexLoginRequired):
                client.read_rate_limits()
        finally:
            client.close()

    def test_child_exit_becomes_fetch_error(self, tmp_path: Path) -> None:
        client = CodexAppServerClient(_config(tmp_path, "immediate_exit"))
        try:
            with pytest.raises(FetchError):
                client.read_account()
        finally:
            client.close()

    def test_timeout_becomes_fetch_error(self, tmp_path: Path) -> None:
        config = _config(tmp_path, request_timeout=0.5)
        session = AppServerSession(config)
        session.start()
        try:
            with pytest.raises(FetchError, match="timed out"):
                session.request("hang")
        finally:
            session.close()

    def test_stderr_is_drained_so_the_child_cannot_deadlock(
        self, tmp_path: Path
    ) -> None:
        # Without a drain thread the child blocks on a full stderr pipe and
        # this call hangs rather than returning.
        client = CodexAppServerClient(_config(tmp_path, "stderr_flood",
                                              request_timeout=20.0))
        try:
            assert client.read_account()["account"]["type"] == "chatgpt"
        finally:
            client.close()


class TestLifetimePolicy:
    """Persistent is the runtime default; one-shot stays supported for enrolment."""

    def test_the_default_is_persistent(self, tmp_path: Path) -> None:
        """Guard rail, not a style preference.

        A child per poll leaks ~29 kB into CODEX_HOME per *start* —
        uncheckpointed SQLite WALs plus a `.tmp/git-*` directory — which is
        ~8.3 MB/day at a 300s interval and fills the 1 GiB PVC in about four
        months, taking the readings DB with it. The growth is per-start, not
        per-request, so one resident child bounds it. Flipping this back
        reintroduces a failure that shows up a third of a year later.
        """
        client = CodexAppServerClient(_config(tmp_path))
        try:
            client.read_account()
            assert client._session is not None, (
                "the runtime default must reuse one child; a child per call "
                "grows CODEX_HOME without bound"
            )
        finally:
            client.close()

    def test_a_persistent_child_is_started_once_across_many_reads(
        self, tmp_path: Path
    ) -> None:
        config = _config(tmp_path)
        client = CodexAppServerClient(config, persistent=True)
        try:
            for _ in range(5):
                client.read_account()
        finally:
            client.close()
        # The fake rewrites invocation.json on every start, so a start count is
        # observable: one file, and the child answered five reads.
        assert (Path(config.home) / "invocation.json").exists()

    def test_one_shot_starts_a_child_per_call_and_retains_none(
        self, tmp_path: Path
    ) -> None:
        client = CodexAppServerClient(_config(tmp_path), persistent=False)
        try:
            client.read_account()
            client.read_account()
            assert client._session is None
        finally:
            client.close()

    def test_persistent_reuses_one_child(self, tmp_path: Path) -> None:
        client = CodexAppServerClient(_config(tmp_path), persistent=True)
        try:
            client.read_account()
            first = client._session
            client.read_account()
            assert client._session is first is not None
        finally:
            client.close()

    def test_persistent_drops_a_broken_child(self, tmp_path: Path) -> None:
        config = _config(tmp_path, request_timeout=0.5)
        client = CodexAppServerClient(config, persistent=True)
        try:
            client.read_account()
            session = client._session
            assert session is not None
            with pytest.raises(FetchError):
                session.request("hang")
                client._session.request("hang")  # type: ignore[union-attr]
        finally:
            client.close()

    def test_close_reaps_the_child(self, tmp_path: Path) -> None:
        client = CodexAppServerClient(_config(tmp_path), persistent=True)
        client.read_account()
        session = client._session
        assert session is not None and session.alive
        client.close()
        assert not session.alive
        assert client._session is None

    def test_close_is_idempotent(self, tmp_path: Path) -> None:
        client = CodexAppServerClient(_config(tmp_path), persistent=False)
        client.close()
        client.close()

    def test_calls_after_close_fail_cleanly(self, tmp_path: Path) -> None:
        client = CodexAppServerClient(_config(tmp_path), persistent=False)
        client.close()
        with pytest.raises(FetchError):
            client.read_account()


class TestDeviceCodeLogin:
    def test_presents_the_code_and_waits_for_completion(self, tmp_path: Path) -> None:
        client = CodexAppServerClient(_config(tmp_path))
        seen: list[tuple[str, str]] = []
        try:
            client.device_code_login(lambda url, code: seen.append((url, code)))
        finally:
            client.close()
        assert seen == [("https://auth.openai.com/codex/device", "L1G2-RW770")]

    def test_timeout_cancels_the_login(self, tmp_path: Path) -> None:
        config = _config(tmp_path, "login_never_completes")
        client = CodexAppServerClient(config)
        try:
            with pytest.raises(FetchError):
                client.device_code_login(lambda url, code: None, timeout=0.5)
        finally:
            client.close()
        # A half-open login must not be left behind for the next attempt.
        cancelled = Path(config.home) / "cancelled"
        assert cancelled.exists()
        assert json.loads(cancelled.read_text())["loginId"] == "login-1"

    def test_no_token_material_crosses_the_boundary(self, tmp_path: Path) -> None:
        # The login returns None: everything the operator needs arrives via
        # the presenter callback, and nothing else escapes.
        client = CodexAppServerClient(_config(tmp_path))
        try:
            assert client.device_code_login(lambda url, code: None) is None
        finally:
            client.close()


class TestSessionHygiene:
    def test_session_cannot_be_started_twice(self, tmp_path: Path) -> None:
        session = AppServerSession(_config(tmp_path))
        session.start()
        try:
            with pytest.raises(FetchError):
                session.start()
        finally:
            session.close()

    def test_codex_home_is_created_if_absent(self, tmp_path: Path) -> None:
        binary = _fake_codex(tmp_path, "ok")
        home = tmp_path / "nested" / "codex-home"
        assert not home.exists()
        client = CodexAppServerClient(
            CodexAppServerConfig(binary=str(binary), home=str(home))
        )
        try:
            client.read_account()
        finally:
            client.close()
        assert home.is_dir()

    def test_requests_before_initialize_are_impossible(self, tmp_path: Path) -> None:
        # The public client only ever hands out started sessions, so the
        # handshake has already happened by the time a caller can request.
        client = CodexAppServerClient(_config(tmp_path))
        try:
            with client._acquire() as session:
                assert session.alive
                # id 1 was consumed by initialize during start().
                assert session._next_id >= 1
        finally:
            client.close()


def test_fake_binary_is_executable(tmp_path: Path) -> None:
    binary = _fake_codex(tmp_path, "ok")
    assert os.access(binary, os.X_OK)
    assert binary.stat().st_mode & stat.S_IXUSR


class TestSchedulerIntegration:
    """A missing login must be legible on the dashboard, not just in the log."""

    def test_login_required_parks_the_tile_with_an_actionable_detail(
        self, tmp_path: Path
    ) -> None:
        from usage_dashboard.server.db import Database
        from usage_dashboard.server.scheduler import FetchScheduler
        from usage_dashboard.shared.models import Provider, ReadingStatus

        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        client = CodexAppServerClient(_config(tmp_path, "logged_out"))
        scheduler = FetchScheduler(db, codex_client=client)
        try:
            # Codex stays *configured* — an expired login must not make the
            # tile silently disappear.
            assert scheduler.configured_providers() == [Provider.CODEX]
            scheduler.fetch_now()
        finally:
            scheduler.stop()

        reading = db.get_latest_readings()[Provider.CODEX]
        assert reading.status is ReadingStatus.OFFLINE
        assert reading.detail is not None
        assert "login codex" in reading.detail

    def test_a_transport_failure_is_not_treated_as_an_auth_failure(
        self, tmp_path: Path
    ) -> None:
        from usage_dashboard.server.db import Database
        from usage_dashboard.server.scheduler import FetchScheduler
        from usage_dashboard.shared.models import Provider

        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        client = CodexAppServerClient(_config(tmp_path, "immediate_exit"))
        scheduler = FetchScheduler(db, codex_client=client)
        try:
            scheduler.fetch_now()
        finally:
            scheduler.stop()
        reading = db.get_latest_readings()[Provider.CODEX]
        # A dead child is a transport problem: it must NOT tell the operator to
        # go and re-enrol, and it must back off normally rather than parking.
        assert reading.detail is None or "login codex" not in reading.detail
