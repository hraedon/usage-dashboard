"""Plan 006 — browser enrolment pane: service, scheduler seams, API routes."""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from usage_dashboard.server import enrolment as enrolment_mod
from usage_dashboard.server.api import create_app
from usage_dashboard.server.db import Database
from usage_dashboard.server.enrolment import (
    EnrolmentError,
    EnrolmentService,
    redact,
)
from usage_dashboard.server.scheduler import FetchScheduler
from usage_dashboard.server.token_store import TokenStore

API_KEY = "test-secret-key"

# An interactive ceremony stand-in: prints a prompt, blocks on one stdin
# line, echoes it back. This is the shape the PTY transport must support —
# streaming output and feeding input — without depending on the real
# `claude` binary.
_FAKE_CEREMONY = """
import sys
print("Open https://claude.example/activate and enter the code shown:")
sys.stdout.flush()
line = sys.stdin.readline()
print("GOT:" + line.strip())
sys.stdout.flush()
"""


def _wait_for(condition, timeout: float = 10.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture()
def ceremony_script(tmp_path: Path) -> Path:
    script = tmp_path / "fake_ceremony.py"
    script.write_text(_FAKE_CEREMONY, encoding="utf-8")
    return script


@pytest.fixture()
def store(tmp_path: Path) -> TokenStore:
    return TokenStore(tmp_path / "tokens.json")


@pytest.fixture()
def service(store: TokenStore, ceremony_script: Path) -> EnrolmentService:
    return EnrolmentService(
        store,
        command_override=[sys.executable, str(ceremony_script)],
    )


class TestRedaction:
    def test_token_runs_are_masked_urls_and_codes_survive(self):
        token = "sk-ant-oat01-" + "x" * 80
        line = f"  https://platform.claude.com/activate?code=AB12-CD34 paste {token}"
        out = redact(line)
        assert "x" * 80 not in out
        assert "<redacted>" in out
        assert "https://platform.claude.com/activate?code=AB12-CD34" in out

    def test_short_values_pass_through(self):
        assert redact("code: AB12-CD34") == "code: AB12-CD34"


class TestClaudeJob:
    def test_streams_transcript_and_feeds_input(self, service: EnrolmentService):
        job = service.start_claude("personal")
        assert job["state"] == "running"

        def prompt_visible() -> bool:
            snap = service.get_job(job["job_id"]).to_dict()
            return any("enter the code" in ln.lower() for ln in snap["transcript"])

        _wait_for(prompt_visible, what="ceremony prompt in transcript")
        service.send_input(job["job_id"], "AB12-CD34")
        _wait_for(
            lambda: service.get_job(job["job_id"]).to_dict()["state"] != "running",
            what="ceremony completion",
        )
        snapshot = service.get_job(job["job_id"]).to_dict()
        assert snapshot["state"] == "succeeded"
        assert any("GOT:AB12-CD34" in ln for ln in snapshot["transcript"])

    def test_rejects_unknown_account(self, service: EnrolmentService):
        with pytest.raises(EnrolmentError, match="personal.*work|account"):
            service.start_claude("side")

    def test_only_one_job_at_a_time(self, service: EnrolmentService):
        first = service.start_claude("personal")
        with pytest.raises(EnrolmentError, match="already running"):
            service.start_claude("work")
        service.cancel(first["job_id"])
        _wait_for(
            lambda: service.get_job(first["job_id"]).to_dict()["state"] != "running",
            what="cancelled job to finish",
        )

    def test_failed_ceremony_reports_exit_status(self, store: TokenStore, tmp_path: Path):
        failing = tmp_path / "fail.py"
        failing.write_text("import sys\nprint('boom')\nsys.exit(3)\n", encoding="utf-8")
        service = EnrolmentService(
            store, command_override=[sys.executable, str(failing)]
        )
        job = service.start_claude("work")
        _wait_for(
            lambda: service.get_job(job["job_id"]).to_dict()["state"] != "running",
            what="failing job to finish",
        )
        snapshot = service.get_job(job["job_id"]).to_dict()
        assert snapshot["state"] == "failed"
        assert snapshot["exit_code"] == 3
        assert "status 3" in (snapshot["error"] or "")


class TestCodexJob:
    def _patch_cli(self, monkeypatch, behaviour) -> None:
        def fake_login_codex(
            codex_home=None, codex_bin=None, timeout=900.0, emit=None, present=None
        ):
            emit("Enrolling Codex against CODEX_HOME=/data/codex")
            present("https://auth.example/device", "WRLD-CODE")
            behaviour(emit)

        monkeypatch.setattr(
            "usage_dashboard.cli.login_codex", fake_login_codex, raising=True
        )

    def test_success_brackets_pause_resume_and_surfaces_code(
        self, store: TokenStore, monkeypatch
    ):
        self._patch_cli(monkeypatch, lambda emit: emit("done"))
        events: list[str] = []
        service = EnrolmentService(
            store,
            codex_home="/data/codex",
            codex_pause=lambda: events.append("pause") or True,
            codex_resume=lambda: events.append("resume") or True,
            on_success=lambda p: events.append(f"success:{p}"),
        )
        job = service.start_codex()
        _wait_for(
            lambda: service.get_job(job["job_id"]).to_dict()["state"] != "running",
            what="codex job to finish",
        )
        snapshot = service.get_job(job["job_id"]).to_dict()
        assert snapshot["state"] == "succeeded"
        assert snapshot["verification_url"] == "https://auth.example/device"
        assert snapshot["user_code"] == "WRLD-CODE"
        assert events == ["pause", "resume", "success:codex"]

    def test_failure_still_resumes(self, store: TokenStore, monkeypatch):
        def blow_up(emit):
            raise SystemExit(1)

        self._patch_cli(monkeypatch, blow_up)
        events: list[str] = []
        service = EnrolmentService(
            store,
            codex_pause=lambda: True,
            codex_resume=lambda: events.append("resume") or True,
            on_success=lambda p: events.append(f"success:{p}"),
        )
        job = service.start_codex()
        _wait_for(
            lambda: service.get_job(job["job_id"]).to_dict()["state"] != "running",
            what="failed codex job to finish",
        )
        snapshot = service.get_job(job["job_id"]).to_dict()
        assert snapshot["state"] == "failed"
        assert events == ["resume"]

    def test_cancel_is_refused(self, store: TokenStore):
        service = EnrolmentService(store)
        job = service._new_job("codex", None)  # register without spawning
        with pytest.raises(EnrolmentError, match="cannot be cancelled"):
            service.cancel(job.job_id)
        service._finish_active(job)


class TestCredentialPaste:
    def test_ollama_verifies_then_stores(self, store: TokenStore, monkeypatch):
        seen: list[str] = []
        monkeypatch.setattr(enrolment_mod, "fetch_ollama_usage", lambda c: seen.append(c))
        events: list[str] = []
        service = EnrolmentService(store, on_success=events.append)
        result = service.submit_credential("ollama", "  session=abc; other=def ")
        assert result == {"status": "stored", "provider": "ollama", "verified": True}
        assert seen == ["session=abc; other=def"]
        assert store.get_credential("ollama") == "session=abc; other=def"
        assert events == ["ollama"]

    def test_opencode_stores_workspace_metadata(self, store: TokenStore, monkeypatch):
        seen: list[tuple[str, str]] = []
        monkeypatch.setattr(
            enrolment_mod, "fetch_opencode_usage", lambda w, c: seen.append((w, c))
        )
        service = EnrolmentService(store)
        service.submit_credential(
            "opencode", "authcookie", workspace_id="wrk_01HATEST0000000000000000000"
        )
        assert seen == [("wrk_01HATEST0000000000000000000", "authcookie")]
        assert store.get_credential("opencode") == "authcookie"
        assert (
            store.get_metadata("opencode", "workspace_id")
            == "wrk_01HATEST0000000000000000000"
        )

    def test_failed_verification_stores_nothing(self, store: TokenStore, monkeypatch):
        def reject(_credential):
            raise RuntimeError("401 denied")

        monkeypatch.setattr(enrolment_mod, "fetch_ollama_usage", reject)
        service = EnrolmentService(store)
        with pytest.raises(EnrolmentError, match="verification failed"):
            service.submit_credential("ollama", "session=stale")
        assert store.get_credential("ollama") is None

    def test_opencode_requires_workspace(self, store: TokenStore):
        service = EnrolmentService(store)
        with pytest.raises(EnrolmentError, match="workspace"):
            service.submit_credential("opencode", "authcookie", workspace_id="  ")

    def test_unknown_provider_rejected(self, store: TokenStore):
        service = EnrolmentService(store)
        with pytest.raises(EnrolmentError, match="ollama.*opencode|provider"):
            service.submit_credential("zai", "key")


class TestSchedulerSeams:
    """update_credentials + the codex pause/resume bracket (Plan 006)."""

    def _scheduler(self, tmp_path: Path, client: Any, factory: Any) -> FetchScheduler:
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        return FetchScheduler(
            db=db,
            codex_client=client,
            codex_factory=factory,
            ollama_cookie="old-cookie",
        )

    def test_update_credentials_swaps_only_known_attrs(self, tmp_path: Path):
        scheduler = self._scheduler(tmp_path, None, None)
        scheduler.update_credentials(ollama_cookie="new-cookie")
        assert scheduler._ollama_cookie == "new-cookie"
        with pytest.raises(ValueError, match="not a credential attribute"):
            scheduler.update_credentials(interval_seconds=1)

    def test_pause_closes_client_and_drops_task(self, tmp_path: Path):
        class FakeClient:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        client = FakeClient()
        made: list[FakeClient] = []

        def factory() -> FakeClient:
            fresh = FakeClient()
            made.append(fresh)
            return fresh

        scheduler = self._scheduler(tmp_path, client, factory)
        assert any(p.value == "codex" for p, _ in scheduler._get_fetch_tasks())
        assert scheduler.pause_codex() is True
        assert client.closed is True
        assert not any(p.value == "codex" for p, _ in scheduler._get_fetch_tasks())
        assert scheduler.pause_codex() is False  # nothing left to pause
        assert scheduler.resume_codex() is True
        assert len(made) == 1
        assert scheduler._codex_client is made[0]
        assert any(p.value == "codex" for p, _ in scheduler._get_fetch_tasks())
        assert scheduler.resume_codex() is False  # no longer paused

    def test_resume_without_factory_refuses(self, tmp_path: Path):
        scheduler = self._scheduler(tmp_path, None, None)
        assert scheduler.pause_codex() is False
        assert scheduler.resume_codex() is False


class TestTokenStoreCredentialMetadata:
    def test_metadata_set_and_removed(self, store: TokenStore):
        store.save_credential("opencode", "c1", metadata={"workspace_id": "wrk_x"})
        assert store.get_metadata("opencode", "workspace_id") == "wrk_x"
        store.save_credential("opencode", "c2", metadata={"workspace_id": None})
        assert store.get_metadata("opencode", "workspace_id") is None
        assert store.get_credential("opencode") == "c2"


class TestLoginApi:
    def _app(self, tmp_path: Path, service: EnrolmentService | None):
        db = Database(str(tmp_path / "api.db"))
        db.initialize()
        return create_app(API_KEY, db, enrolment=service)

    def _client(self, app):
        transport = httpx.ASGITransport(app=app)
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    def _get(self, app, path, key=API_KEY):
        async def run():
            async with self._client(app) as client:
                headers = {"Authorization": f"Bearer {key}"} if key else {}
                return await client.get(path, headers=headers)

        return asyncio.run(run())

    def test_login_page_shell_is_served(self, tmp_path: Path):
        response = self._get(self._app(tmp_path, None), "/login", key=None)
        assert response.status_code == 200
        assert "Enrolment pane" in response.text
        assert "sessionStorage" in response.text

    def test_status_requires_bearer_key(self, tmp_path: Path):
        response = self._get(self._app(tmp_path, None), "/internal/v1/login/status", key=None)
        assert response.status_code == 401

    def test_status_without_service_reports_501(self, tmp_path: Path):
        response = self._get(self._app(tmp_path, None), "/internal/v1/login/status")
        assert response.status_code == 501

    def test_status_with_service(self, tmp_path: Path, service: EnrolmentService):
        response = self._get(self._app(tmp_path, service), "/internal/v1/login/status")
        assert response.status_code == 200
        body = response.json()
        assert body["active"] is None
        assert body["providers"]["claude"]["accounts"] == ["personal", "work"]

    def test_claude_job_round_trip(self, tmp_path: Path, service: EnrolmentService):
        app = self._app(tmp_path, service)

        async def run():
            async with self._client(app) as client:
                headers = {"Authorization": f"Bearer {API_KEY}"}
                started = await client.post(
                    "/internal/v1/login/claude",
                    json={"account": "work"},
                    headers=headers,
                )
                assert started.status_code == 200
                job_id = started.json()["job_id"]

                async def poll_job() -> dict[str, Any]:
                    response = await client.get(
                        f"/internal/v1/login/jobs/{job_id}", headers=headers
                    )
                    assert response.status_code == 200
                    return response.json()

                for _ in range(200):
                    job = await poll_job()
                    if any("enter the code" in ln.lower() for ln in job["transcript"]):
                        break
                    await asyncio.sleep(0.05)
                sent = await client.post(
                    f"/internal/v1/login/jobs/{job_id}/input",
                    json={"text": "AB12-CD34"},
                    headers=headers,
                )
                assert sent.status_code == 200
                for _ in range(200):
                    job = await poll_job()
                    if job["state"] != "running":
                        break
                    await asyncio.sleep(0.05)
                assert job["state"] == "succeeded"
                unknown = await client.get(
                    "/internal/v1/login/jobs/nope", headers=headers
                )
                assert unknown.status_code == 404

        asyncio.run(run())

    def test_credential_endpoint_rejects_failed_verification(
        self, tmp_path: Path, service: EnrolmentService, monkeypatch
    ):
        def reject(_credential):
            raise RuntimeError("403")

        monkeypatch.setattr(enrolment_mod, "fetch_ollama_usage", reject)
        app = self._app(tmp_path, service)

        async def run():
            async with self._client(app) as client:
                headers = {"Authorization": f"Bearer {API_KEY}"}
                response = await client.post(
                    "/internal/v1/login/credential",
                    json={"provider": "ollama", "credential": "x"},
                    headers=headers,
                )
                assert response.status_code == 400
                assert "verification failed" in response.json()["detail"]

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Plan 007 — Antigravity on the PTY transport
# ---------------------------------------------------------------------------

# 43 token-charset chars, like a real PKCE challenge: long enough that the
# transcript redactor masks it, which is exactly why the link has to be lifted
# into a structured field before redaction.
_PKCE = "JafNPs6Tjm1RKUa8tAy_w_6jvAEUOwHzfjrm2tb2Gbs"
_SIGNIN_URL = (
    "https://accounts.google.com/o/oauth2/auth?access_type=offline"
    "&client_id=000000000000-fakefakefakefakefakefakefakefake.apps.googleusercontent.com"
    f"&code_challenge={_PKCE}&code_challenge_method=S256&state=abc123"
)

# What agy 1.4.12 prints signed out (stderr), then the wrapper's tail.
_FAKE_AGY_CEREMONY = f"""
import sys
sys.stderr.write("Authentication required. Please visit the URL to log in:\\n")
sys.stderr.write("  {_SIGNIN_URL}\\n\\n")
sys.stderr.write("Waiting for authentication (timeout 60s)...\\n")
sys.stderr.write("Or, paste the authorization code here and press Enter:\\n")
sys.stderr.flush()
line = sys.stdin.readline()
print("GOT:" + line.strip())
"""


@pytest.fixture()
def agy_service(store: TokenStore, tmp_path: Path) -> tuple[EnrolmentService, list[str]]:
    script = tmp_path / "fake_agy_ceremony.py"
    script.write_text(_FAKE_AGY_CEREMONY, encoding="utf-8")
    notified: list[str] = []
    service = EnrolmentService(
        store,
        antigravity_home=str(tmp_path / "antigravity"),
        antigravity_command_override=[sys.executable, str(script)],
        on_success=notified.append,
    )
    return service, notified


class TestAntigravityJob:
    def test_link_and_countdown_survive_redaction(self, agy_service):
        service, notified = agy_service
        job = service.start_antigravity()
        assert job["provider"] == "antigravity"

        def prompted() -> bool:
            return service.get_job(job["job_id"]).to_dict()["input_seconds_left"] is not None

        _wait_for(prompted, what="paste-window announcement")
        snap = service.get_job(job["job_id"]).to_dict()
        # The structured field keeps the link usable...
        assert snap["verification_url"] == _SIGNIN_URL
        # ...while the transcript copy stays redacted.
        transcript = "\n".join(snap["transcript"])
        assert _PKCE not in transcript
        assert "<redacted>" in transcript
        assert 55 <= snap["input_seconds_left"] <= 60
        assert snap["accepts_input"] is True
        assert snap["user_code"] is None  # no device code: the pane shows a button

        service.send_input(job["job_id"], "4/0AVG-one-time-code")
        _wait_for(
            lambda: service.get_job(job["job_id"]).to_dict()["state"] != "running",
            what="ceremony completion",
        )
        done = service.get_job(job["job_id"]).to_dict()
        assert done["state"] == "succeeded"
        assert done["accepts_input"] is False
        assert done["input_seconds_left"] is None
        assert notified == ["antigravity"]

    def test_only_a_google_oauth_link_is_lifted(self, store: TokenStore, tmp_path: Path):
        script = tmp_path / "evil.py"
        script.write_text(
            "print('Visit https://evil.example/o/oauth2/auth?x=1')\n"
            "print('or https://accounts.google.com.evil.example/o/oauth2/auth?x=1')\n",
            encoding="utf-8",
        )
        service = EnrolmentService(
            store,
            antigravity_home=str(tmp_path / "antigravity"),
            antigravity_command_override=[sys.executable, str(script)],
        )
        job = service.start_antigravity()
        _wait_for(
            lambda: service.get_job(job["job_id"]).to_dict()["state"] != "running",
            what="job end",
        )
        assert service.get_job(job["job_id"]).to_dict()["verification_url"] is None

    def test_claude_jobs_never_lift_links(self, service: EnrolmentService):
        # The lifting is Antigravity-only; Claude's transcript is unchanged.
        job = service.start_claude("personal")
        service.cancel(job["job_id"])
        _wait_for(
            lambda: service.get_job(job["job_id"]).to_dict()["state"] != "running",
            what="cancel",
        )
        assert service.get_job(job["job_id"]).to_dict()["input_seconds_left"] is None

    def test_disabled_server_neither_offers_nor_starts_it(self, store: TokenStore):
        service = EnrolmentService(store)
        assert "antigravity" not in service.status()["providers"]
        with pytest.raises(EnrolmentError, match="ANTIGRAVITY_MODE"):
            service.start_antigravity()

    def test_enabled_server_offers_it_as_interactive(self, agy_service):
        service, _ = agy_service
        assert service.status()["providers"]["antigravity"] == {"mode": "interactive"}

    def test_real_command_runs_the_cli_module(self, store: TokenStore):
        service = EnrolmentService(
            store, antigravity_home="/data/antigravity", agy_bin="/usr/local/bin/agy"
        )
        assert service._antigravity_command("/data/antigravity") == [
            sys.executable, "-m", "usage_dashboard.cli", "login", "antigravity",
            "--antigravity-home", "/data/antigravity",
            "--agy-bin", "/usr/local/bin/agy",
        ]


class TestAntigravityLoginApi:
    """The route is INTERNAL_ONLY and bearer-gated like the other ceremonies."""

    _app = TestLoginApi._app
    _client = TestLoginApi._client
    _get = TestLoginApi._get

    def test_round_trip(self, tmp_path: Path, agy_service):
        service, _ = agy_service
        app = self._app(tmp_path, service)

        async def run():
            async with self._client(app) as client:
                headers = {"Authorization": f"Bearer {API_KEY}"}
                denied = await client.post("/internal/v1/login/antigravity")
                assert denied.status_code == 401
                started = await client.post(
                    "/internal/v1/login/antigravity", headers=headers
                )
                assert started.status_code == 200
                job_id = started.json()["job_id"]
                for _ in range(200):
                    job = (
                        await client.get(
                            f"/internal/v1/login/jobs/{job_id}", headers=headers
                        )
                    ).json()
                    if job["verification_url"]:
                        break
                    await asyncio.sleep(0.05)
                assert job["verification_url"] == _SIGNIN_URL
                again = await client.post(
                    "/internal/v1/login/antigravity", headers=headers
                )
                assert again.status_code == 409  # one ceremony at a time
                await client.post(
                    f"/internal/v1/login/jobs/{job_id}/input",
                    json={"text": "code"},
                    headers=headers,
                )
                for _ in range(200):
                    job = (
                        await client.get(
                            f"/internal/v1/login/jobs/{job_id}", headers=headers
                        )
                    ).json()
                    if job["state"] != "running":
                        break
                    await asyncio.sleep(0.05)
                assert job["state"] == "succeeded"

        asyncio.run(run())

    def test_disabled_server_refuses(self, tmp_path: Path, store: TokenStore):
        app = self._app(tmp_path, EnrolmentService(store))

        async def run():
            async with self._client(app) as client:
                response = await client.post(
                    "/internal/v1/login/antigravity",
                    headers={"Authorization": f"Bearer {API_KEY}"},
                )
                assert response.status_code == 409
                assert "ANTIGRAVITY_MODE" in response.json()["detail"]

        asyncio.run(run())

    def test_pane_carries_the_antigravity_controls(self, tmp_path: Path):
        page = self._get(self._app(tmp_path, None), "/login", key=None).text
        assert 'id="agy-card"' in page
        assert "/internal/v1/login/antigravity" in page
        assert "Open Google sign-in" in page
        # The paste box follows the job's PTY, not a hard-coded provider.
        assert "job.accepts_input" in page


def test_input_is_refused_once_the_pty_is_detached(store: TokenStore, tmp_path: Path):
    """Plan 007 review: a paste racing ceremony exit must never be written to
    a closed (and possibly reused) descriptor."""
    job = enrolment_mod.EnrolmentJob("job-x", "antigravity", None)
    read_fd, write_fd = os.pipe()
    try:
        job.attach_pty(write_fd, None)  # type: ignore[arg-type]
        job.write_input("first")
        assert os.read(read_fd, 64) == b"first\n"
        job.detach_pty()
        assert job.to_dict()["accepts_input"] is False
        with pytest.raises(EnrolmentError, match="not accepting input"):
            job.write_input("late")
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_ceremony_exit_detaches_before_closing_the_master(
    store: TokenStore, tmp_path: Path, monkeypatch
):
    script = tmp_path / "quick.py"
    script.write_text("print('bye')\n", encoding="utf-8")
    service = EnrolmentService(
        store,
        antigravity_home=str(tmp_path / "antigravity"),
        antigravity_command_override=[sys.executable, str(script)],
    )
    real_close = os.close
    closed_while_attached: list[int] = []
    jobs: list[enrolment_mod.EnrolmentJob] = []

    def checking_close(fd: int) -> None:
        for job in jobs:
            if job._master_fd == fd:
                closed_while_attached.append(fd)
        real_close(fd)

    monkeypatch.setattr(enrolment_mod.os, "close", checking_close)
    started = service.start_antigravity()
    jobs.append(service.get_job(started["job_id"]))
    _wait_for(lambda: jobs[0].to_dict()["state"] != "running", what="exit")
    assert closed_while_attached == []
