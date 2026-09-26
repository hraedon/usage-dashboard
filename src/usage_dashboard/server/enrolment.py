"""Browser-driven enrolment (Plan 006).

Runs the provider enrolment ceremonies behind HTTP so the operator can drive
them from the ``/login`` pane instead of ``kubectl exec`` into the single RWO
pod. The CLI remains the ceremony owner: Claude enrolment is the CLI module
spawned as a subprocess on a PTY (the official login expects a terminal and a
pasted-back code), and Codex enrolment is ``cli.login_codex`` called directly
with injected output callbacks, because that flow has no terminal dependency.

Serialization is deliberate. One active job at a time: the token store is one
JSON file, a Claude credential family must have exactly one refresher, and
the Codex ceremony needs exclusive ``CODEX_HOME`` ownership. Concurrent
ceremonies would violate all three, so the service refuses a second start
while one runs.

Transcripts are defence-in-depth redacted: the ceremonies print URLs and
one-time codes (which must stay visible to be useful) but never token
material; anything token-shaped that a future CLI version might print is
masked before it reaches the API or the browser.
"""
from __future__ import annotations

import logging
import os
import pty
import re
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from usage_dashboard.server.fetch_ollama import fetch_ollama_usage
from usage_dashboard.server.fetch_opencode import fetch_opencode_usage
from usage_dashboard.server.token_store import TokenStore

logger = logging.getLogger(__name__)

# Long runs of token-charset characters. Real URLs stay intact: their
# `://`, `/`, `.` and `?` break the run into short pieces, while OAuth
# access tokens, refresh tokens and session-cookie values do not contain
# any of those characters and routinely exceed 40 chars. One-time codes
# (the thing the operator must read) are short by design and survive.
_TOKEN_RUN = re.compile(r"[A-Za-z0-9_-]{40,}")

# ANSI escape sequences (CUU/CUD/EL/SGR/OSC…): a PTY makes the official CLI
# believe a terminal is present, so it may emit cursor addressing and colour.
_ANSI_ESCAPE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]" r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)" r"|\x1b[@-_]"
)

_REDACTED = "<redacted>"

# How many transcript lines to keep per job — the pane polls, so this is the
# window an operator who looked away can scroll back through.
_TRANSCRIPT_MAX_LINES = 400

# Finished jobs kept for inspection (the pane shows the last one; a short
# history keeps a failure reportable after a retry succeeds).
_JOB_HISTORY = 8

# Provider capability matrix surfaced by /login/status so the pane renders
# each provider with the interaction it actually supports.
CAPABILITIES: dict[str, dict[str, Any]] = {
    "claude": {"mode": "interactive", "accounts": ["personal", "work"]},
    "codex": {"mode": "device-code"},
    "ollama": {"mode": "credential-paste"},
    "opencode": {"mode": "credential-paste", "fields": ["credential", "workspace_id"]},
}


class EnrolmentError(Exception):
    """An enrolment request could not be started, continued or accepted."""


def redact(text: str) -> str:
    """Mask token-shaped runs while keeping URLs and one-time codes readable."""
    return _TOKEN_RUN.sub(_REDACTED, text)


class EnrolmentJob:
    """One ceremony run: lifecycle state plus the streamed transcript.

    ``_lock`` guards the mutable fields and the PTY master fd; ``emit`` is
    called from the PTY reader thread while ``to_dict`` is called from API
    threads, so every read of a mutating field goes through it.
    """

    def __init__(self, job_id: str, provider: str, account: str | None) -> None:
        self.job_id = job_id
        self.provider = provider
        self.account = account
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.state = "running"
        self.exit_code: int | None = None
        self.error: str | None = None
        self.verification_url: str | None = None
        self.user_code: str | None = None
        self.transcript: deque[str] = deque(maxlen=_TRANSCRIPT_MAX_LINES)
        self._lock = threading.Lock()
        self._master_fd: int | None = None
        self._process: subprocess.Popen[bytes] | None = None

    def emit(self, line: str) -> None:
        """Append one (redacted, escape-stripped) line to the transcript."""
        cleaned = _ANSI_ESCAPE.sub("", line).replace("\r", "").rstrip()
        if not cleaned:
            return
        with self._lock:
            self.transcript.append(redact(cleaned))

    def set_device_code(self, url: str, code: str) -> None:
        with self._lock:
            self.verification_url = url
            self.user_code = code

    def attach_pty(self, master_fd: int, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._master_fd = master_fd
            self._process = process

    def write_input(self, text: str) -> None:
        """Feed one operator line to the ceremony's stdin (the PTY master)."""
        with self._lock:
            fd = self._master_fd
        if fd is None:
            raise EnrolmentError("job is not accepting input")
        data = text.rstrip("\n") + "\n"
        try:
            os.write(fd, data.encode("utf-8"))
        except OSError as exc:
            raise EnrolmentError(f"could not write to the ceremony: {exc}") from exc

    def terminate(self) -> None:
        """Cancel a running subprocess ceremony (SIGTERM, then reap)."""
        with self._lock:
            process = self._process
        if process is None or process.poll() is not None:
            raise EnrolmentError("job is not running")
        process.terminate()

    def finish(self, exit_code: int, error: str | None = None) -> None:
        with self._lock:
            if self.state != "running":
                return
            self.state = "succeeded" if exit_code == 0 else "failed"
            self.exit_code = exit_code
            self.error = error
            self.finished_at = time.time()
            self._master_fd = None

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "job_id": self.job_id,
                "provider": self.provider,
                "account": self.account,
                "state": self.state,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "exit_code": self.exit_code,
                "error": self.error,
                "verification_url": self.verification_url,
                "user_code": self.user_code,
                "transcript": list(self.transcript),
            }


class EnrolmentService:
    """Serialized, in-pod execution of the enrolment ceremonies (Plan 006).

    ``on_success`` receives the provider name once a ceremony has committed a
    fresh credential — the wiring in ``main`` reloads scheduler credentials
    and forces a fetch, which is what makes enrolment take effect without a
    rollout. ``codex_pause``/``codex_resume`` bracket the Codex ceremony so
    only one App Server process ever owns ``CODEX_HOME``.
    """

    def __init__(
        self,
        token_store: TokenStore,
        *,
        claude_bin: str = "claude",
        codex_home: str | None = None,
        codex_bin: str | None = None,
        python_bin: str = sys.executable,
        on_success: Callable[[str], None] | None = None,
        codex_pause: Callable[[], bool] | None = None,
        codex_resume: Callable[[], bool] | None = None,
        command_override: list[str] | None = None,
    ) -> None:
        self._token_store = token_store
        self._claude_bin = claude_bin
        self._codex_home = codex_home
        self._codex_bin = codex_bin
        self._python_bin = python_bin
        self._on_success = on_success
        self._codex_pause = codex_pause
        self._codex_resume = codex_resume
        self._command_override = command_override
        self._lock = threading.Lock()
        self._jobs: dict[str, EnrolmentJob] = {}
        self._active_id: str | None = None

    # ------------------------------------------------------------------
    # Job bookkeeping
    # ------------------------------------------------------------------

    def _new_job(self, provider: str, account: str | None) -> EnrolmentJob:
        """Register a job, refusing if one is already running."""
        with self._lock:
            if self._active_id is not None:
                active = self._jobs.get(self._active_id)
                if active is not None and active.state == "running":
                    raise EnrolmentError(
                        f"an enrolment is already running (job {self._active_id}, "
                        f"{active.provider}); wait for it to finish or cancel it"
                    )
            job = EnrolmentJob(
                job_id=f"job-{secrets.token_hex(4)}", provider=provider, account=account
            )
            self._jobs[job.job_id] = job
            self._active_id = job.job_id
            # Bound the finished-job history: drop the oldest finished jobs
            # beyond the keep window, never the active one.
            finished = [j for j in self._jobs.values() if j.state != "running"]
            for stale in finished[:-_JOB_HISTORY] if len(finished) > _JOB_HISTORY else []:
                self._jobs.pop(stale.job_id, None)
            return job

    def _finish_active(self, job: EnrolmentJob) -> None:
        with self._lock:
            if self._active_id == job.job_id:
                self._active_id = None

    def get_job(self, job_id: str) -> EnrolmentJob:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise EnrolmentError(f"unknown job {job_id!r}")
        return job

    def status(self) -> dict[str, Any]:
        with self._lock:
            active = self._jobs.get(self._active_id) if self._active_id else None
            finished = [j for j in self._jobs.values() if j.state != "running"]
            last = finished[-1] if finished else None
            return {
                "active": active.to_dict() if active else None,
                "last": last.to_dict() if last else None,
                "providers": CAPABILITIES,
            }

    # ------------------------------------------------------------------
    # Claude (interactive, PTY-driven CLI subprocess)
    # ------------------------------------------------------------------

    def _claude_command(self, account: str) -> list[str]:
        if self._command_override is not None:
            # Test seam: an arbitrary interactive command in place of the
            # CLI invocation, with the account appended.
            return [*self._command_override, account]
        return [
            self._python_bin,
            "-m",
            "usage_dashboard.cli",
            "login",
            "claude",
            "--account",
            account,
            "--token-store",
            str(self._token_store.path),
            "--claude-bin",
            self._claude_bin,
        ]

    def start_claude(self, account: str) -> dict[str, Any]:
        """Run the official Claude Code login ceremony on a PTY."""
        if account not in ("personal", "work"):
            raise EnrolmentError("account must be 'personal' or 'work'")
        command = self._claude_command(account)
        job = self._new_job("claude", account)
        job.emit(f"$ {' '.join(command)}")
        try:
            master, slave = pty.openpty()
        except OSError as exc:
            job.finish(1, f"could not allocate a PTY: {exc}")
            self._finish_active(job)
            raise EnrolmentError(str(exc)) from exc
        env = dict(os.environ)
        env["TOKEN_STORE_PATH"] = str(self._token_store.path)
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                command,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                env=env,
                close_fds=True,
            )
        except OSError as exc:
            os.close(master)
            os.close(slave)
            job.finish(1, f"could not start the enrolment CLI: {exc}")
            self._finish_active(job)
            raise EnrolmentError(str(exc)) from exc
        os.close(slave)
        job.attach_pty(master, process)
        threading.Thread(
            target=self._pump_pty, args=(job, process, master), daemon=True
        ).start()
        threading.Thread(
            target=self._watch_process, args=(job, process, master), daemon=True
        ).start()
        return job.to_dict()

    def _pump_pty(self, job: EnrolmentJob, process: subprocess.Popen[bytes], master: int) -> None:
        """Stream PTY output into the transcript until the child side closes."""
        pending = b""
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                # EIO is how a PTY master reports the child side gone.
                break
            if not chunk:
                break
            pending += chunk
            while b"\n" in pending:
                raw, pending = pending.split(b"\n", 1)
                job.emit(raw.decode("utf-8", "replace"))
        if pending:
            job.emit(pending.decode("utf-8", "replace"))
        job.emit(f"[process exited, fd {master} closed]")

    def _watch_process(
        self, job: EnrolmentJob, process: subprocess.Popen[bytes], master: int
    ) -> None:
        exit_code = process.wait()
        try:
            os.close(master)
        except OSError:
            pass
        if exit_code == 0:
            job.emit("Enrolment CLI exited successfully.")
        else:
            job.emit(f"Enrolment CLI exited with status {exit_code}.")
            job.finish(exit_code or 1, f"exited with status {exit_code}")
            self._finish_active(job)
            return
        job.finish(0)
        self._finish_active(job)
        self._notify_success(job.provider)

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job.provider == "codex":
            raise EnrolmentError(
                "codex enrolment cannot be cancelled; it times out on its own "
                "and keeps CODEX_HOME exclusive until then"
            )
        job.terminate()
        return {"status": "cancelling", "job_id": job_id}

    def send_input(self, job_id: str, text: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job.state != "running":
            raise EnrolmentError("job is not running")
        job.write_input(text)
        return {"status": "ok", "job_id": job_id}

    # ------------------------------------------------------------------
    # Codex (device-code, App Server exclusive)
    # ------------------------------------------------------------------

    def start_codex(self) -> dict[str, Any]:
        """Run the device-code ceremony with CODEX_HOME exclusively owned."""
        job = self._new_job("codex", None)
        paused = False
        if self._codex_pause is not None:
            try:
                paused = self._codex_pause()
            except Exception:  # noqa: BLE001 - never strand the job on bracketing
                logger.exception("codex pause failed; enrolment proceeds guarded")
        if paused:
            job.emit("Paused the running Codex App Server; enrolment owns CODEX_HOME.")
        threading.Thread(
            target=self._run_codex_job, args=(job, paused), daemon=True
        ).start()
        return job.to_dict()

    def _run_codex_job(self, job: EnrolmentJob, paused: bool) -> None:
        try:
            # Late import keeps this the monkeypatch seam for tests and lets
            # api.py import this module without pulling the CLI's deps.
            from usage_dashboard.cli import login_codex

            def present(url: str, code: str) -> None:
                job.set_device_code(url, code)
                job.emit("Open this URL and enter the code:")
                job.emit(f"  {url}")
                job.emit(f"  code: {code}")
                job.emit("Waiting for the login to complete…")

            login_codex(
                codex_home=self._codex_home,
                codex_bin=self._codex_bin,
                emit=job.emit,
                present=present,
            )
        except SystemExit as exc:
            job.finish(1, f"login_codex exited: {exc.code}")
        except BaseException as exc:  # noqa: BLE001 - the bracket must survive anything
            job.finish(1, f"{type(exc).__name__}: {exc}")
            logger.exception("codex enrolment job failed")
        else:
            job.emit("Codex enrolment CLI completed.")
            job.finish(0)
        finally:
            if paused and self._codex_resume is not None:
                try:
                    self._codex_resume()
                    job.emit("Codex App Server client resumed.")
                except Exception:  # noqa: BLE001 - report, but the job outcome stands
                    logger.exception("codex resume failed after enrolment")
                    job.emit("WARNING: resume failed; the Codex tile stays paused.")
            self._finish_active(job)
        if job.state == "succeeded":
            self._notify_success(job.provider)

    # ------------------------------------------------------------------
    # Ollama / OpenCode (verify-then-store paste)
    # ------------------------------------------------------------------

    def submit_credential(
        self, provider: str, credential: str, workspace_id: str | None = None
    ) -> dict[str, Any]:
        """Verify an operator-pasted session credential, then store it.

        Verification runs the real fetcher first: a credential that cannot do
        the dashboard's one job never displaces a working one (the same rule
        as ``login_claude``'s usage-access check).
        """
        if provider not in ("ollama", "opencode"):
            raise EnrolmentError("provider must be 'ollama' or 'opencode'")
        credential = credential.strip()
        if not credential:
            raise EnrolmentError("credential is empty")
        metadata: dict[str, str] | None = None
        if provider == "ollama":
            try:
                fetch_ollama_usage(credential)
            except Exception as exc:  # noqa: BLE001 - any fetch failure rejects
                raise EnrolmentError(f"verification failed: {exc}") from exc
        else:
            workspace = (workspace_id or "").strip()
            if not workspace:
                raise EnrolmentError(
                    "opencode needs the workspace id (wrk_…) alongside the cookie"
                )
            try:
                fetch_opencode_usage(workspace, credential)
            except Exception as exc:  # noqa: BLE001
                raise EnrolmentError(f"verification failed: {exc}") from exc
            metadata = {"workspace_id": workspace}
        self._token_store.save_credential(provider, credential, metadata=metadata)
        self._notify_success(provider)
        return {"status": "stored", "provider": provider, "verified": True}

    # ------------------------------------------------------------------

    def _notify_success(self, provider: str) -> None:
        if self._on_success is None:
            return
        try:
            self._on_success(provider)
        except Exception:  # noqa: BLE001 - enrolment succeeded; reload is best-effort
            logger.exception("post-enrolment reload failed for %s", provider)
