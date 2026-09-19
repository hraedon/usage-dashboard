"""Client for the official Codex App Server (Plan 005).

Replaces the dashboard's custom ChatGPT OAuth integration. The `codex` CLI
owns the OAuth flow, token persistence and refresh; this module only speaks
its JSONL protocol over stdio and asks for account state and rate limits.
Nothing here mints, stores or transmits an OpenAI token.

Protocol facts below were verified against a real ``codex-cli 0.155.1``
binary rather than taken from documentation:

- ``codex -s read-only -a never app-server`` is accepted in that flag order.
  The adapter never starts a thread or runs a tool, so the sandbox/approval
  flags are belt-and-braces, not load-bearing.
- The handshake is one ``initialize`` request followed by one ``initialized``
  notification. ``experimentalApi`` is **not** needed: the ``account/*``
  methods are on the default surface. (The ``app-server`` *subcommand* is
  nonetheless labelled ``[experimental]`` by the CLI, which is why the image
  pins an exact Codex version.)
- **Responses can arrive out of order.** A live probe issued ``account/read``
  (id 2) then ``account/rateLimits/read`` (id 3) and got id 3 back first.
  Never assume the next line belongs to the last request — correlate on id.
- Unrelated notifications (e.g. ``remoteControl/status/changed``) interleave
  with responses, so every read loop must tolerate them.
- With no login, ``account/read`` returns ``{"account": null,
  "requiresOpenaiAuth": true}`` and ``account/rateLimits/read`` returns a
  JSON-RPC error ``-32600 "codex account authentication required …"``.
- ``account/login/start`` with ``{"type": "chatgptDeviceCode"}`` returns
  ``{loginId, verificationUrl, userCode}``; ``account/login/cancel`` takes the
  ``loginId`` and answers ``{"status": "canceled"}``.

**Process lifetime.** The default is one long-lived child (``persistent=True``),
restarted at most once per call if its transport breaks.

On latency alone a short-lived child per call is fine — a full spawn →
initialize → read cycle measures ~0.7 s against a 300 s poll. What rules it out
is disk. Each *start* leaves roughly 29 kB behind in ``CODEX_HOME``: SQLite
``-wal`` files that are never checkpointed, because the child is terminated
rather than closed cleanly, plus one leaked ``.tmp/git-XXXXXX/`` directory.
Measured over 25 starts that is linear, and at 288 polls a day it works out to
~8.3 MB/day — which fills the 1 GiB Longhorn PVC in about four months and takes
the readings database down with it, four months after anyone touched this code.

The growth is per-start, not per-request: 30 request cycles against one
long-lived child left ``CODEX_HOME`` unchanged at 2.872 MB. So a resident child
costs ~300 MB RSS and bounds the disk usage, while a per-call child costs no RSS
and grows without limit. On a 32 GB node the RSS is the cheaper of the two.

``persistent=False`` is still implemented and tested, and remains correct for a
short-lived or one-shot invocation — the enrolment CLI uses it deliberately, so
that verification runs against a *fresh* process. The session layer is
lifetime-agnostic; the policy is a constructor flag either way.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Callable, Iterator

from usage_dashboard.server.fetch_types import FetchAuthError, FetchError

logger = logging.getLogger(__name__)

DEFAULT_CODEX_BIN = "codex"
DEFAULT_CODEX_HOME = "/data/codex"
# `app-server` last so the global flags bind to the CLI, not the subcommand.
#
# The plugin features are disabled deliberately. Once authenticated, the App
# Server bootstraps a remote plugin catalog (a ~22 MB JSON) and a template cache
# (~25 MB of .pptx/.docx) into CODEX_HOME. This client only ever calls
# `account/read` and `account/rateLimits/read` — it never starts a thread or
# runs a tool — so none of that is reachable, and it is 50 MB of a 1 GiB PVC
# shared with the readings database. Measured: 55.9 MB with plugins, 3.1 MB
# without, identical readings.
DEFAULT_CODEX_ARGS: tuple[str, ...] = (
    "-s",
    "read-only",
    "-a",
    "never",
    "--disable",
    "plugins",
    "--disable",
    "remote_plugin",
    "--disable",
    "plugin_sharing",
    "app-server",
)

_CLIENT_NAME = "usage_dashboard"
_CLIENT_TITLE = "Usage Dashboard"
_CLIENT_VERSION = "0.1.0"

# How long any single request may take before the child is declared broken.
_DEFAULT_REQUEST_TIMEOUT = 30.0
# Grace given to a terminating child before it is killed outright.
_TERMINATE_GRACE = 5.0
# Keep only a short stderr tail for diagnostics; the child must never be able
# to grow our memory without bound, and its output may carry account detail.
_STDERR_TAIL_LINES = 20


class CodexLoginRequired(FetchAuthError):
    """Codex has no usable ChatGPT login, so the operator must enrol.

    A :class:`FetchAuthError` subclass for the scheduler's existing taxonomy,
    but there is nothing for the dashboard to refresh — Codex owns the tokens
    now. The scheduler's Codex refresh path is gone, so this surfaces as a
    normal failure with an actionable message.
    """


@dataclass(frozen=True)
class CodexAppServerConfig:
    """How to launch the App Server child."""

    binary: str = DEFAULT_CODEX_BIN
    home: str = DEFAULT_CODEX_HOME
    args: tuple[str, ...] = DEFAULT_CODEX_ARGS
    request_timeout: float = _DEFAULT_REQUEST_TIMEOUT


@dataclass
class _Pending:
    """Buffered protocol traffic that arrived before it was asked for."""

    responses: dict[int, dict[str, Any]] = field(default_factory=dict)
    notifications: deque[dict[str, Any]] = field(default_factory=deque)


class AppServerSession:
    """One running App Server child plus JSONL framing over its stdio.

    Deliberately knows nothing about how long it should live: it starts,
    answers requests, and closes. The lifetime policy lives in
    :class:`CodexAppServerClient`, which is what makes a future switch to a
    long-lived child a policy change rather than a rewrite.

    Not internally synchronised — the owning client serialises access.
    """

    def __init__(self, config: CodexAppServerConfig) -> None:
        self._config = config
        self._proc: subprocess.Popen[str] | None = None
        self._next_id = 0
        self._pending = _Pending()
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._stderr_thread: threading.Thread | None = None
        # stdout is pumped by a reader thread rather than read with select().
        # select() reports readiness on the file DESCRIPTOR, but readline()
        # buffers: when the child writes a notification and a response
        # together, both land in Python's buffer, readline() returns the
        # notification, and select() then says "nothing to read" while the
        # response sits in memory — the request times out holding its own
        # answer. The real binary does exactly that interleaving.
        self._stdout_queue: queue.Queue[str | None] = queue.Queue()
        self._stdout_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the child and complete the initialize/initialized handshake."""
        if self._proc is not None:
            raise FetchError("Codex App Server session already started")

        # CODEX_HOME is how the child finds (and persists) its own credentials;
        # it must be the same directory across runtime and enrolment or the
        # login simply will not be visible to the dashboard.
        env = dict(os.environ)
        env["CODEX_HOME"] = self._config.home
        try:
            os.makedirs(self._config.home, exist_ok=True)
        except OSError as exc:
            raise FetchError(f"Codex home {self._config.home!r} unusable: {exc}") from exc

        argv = [self._config.binary, *self._config.args]
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
        except OSError as exc:
            raise FetchError(f"Codex App Server failed to start ({argv[0]}): {exc}") from exc

        self._start_stderr_drain()
        self._start_stdout_pump()
        try:
            self._handshake()
        except Exception:
            # A half-initialised child must not be left running.
            self.close()
            raise

    def _start_stderr_drain(self) -> None:
        """Continuously drain stderr so the child cannot block on a full pipe.

        Only a short tail is retained, and it is never echoed into application
        logs at INFO: the child's diagnostics can name the signed-in account.
        """
        proc = self._proc
        if proc is None or proc.stderr is None:
            return

        def _drain() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                self._stderr_tail.append(line.rstrip("\n"))

        thread = threading.Thread(target=_drain, name="codex-app-server-stderr", daemon=True)
        thread.start()
        self._stderr_thread = thread

    def _start_stdout_pump(self) -> None:
        """Move stdout lines into a queue so reads can be bounded by time."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return

        def _pump() -> None:
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    self._stdout_queue.put(line)
            except (OSError, ValueError):
                pass
            finally:
                self._stdout_queue.put(None)  # EOF sentinel

        thread = threading.Thread(target=_pump, name="codex-app-server-stdout", daemon=True)
        thread.start()
        self._stdout_thread = thread

    def _handshake(self) -> None:
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": _CLIENT_NAME,
                    "title": _CLIENT_TITLE,
                    "version": _CLIENT_VERSION,
                }
            },
        )
        self.notify("initialized", {})

    def close(self) -> None:
        """Terminate and reap the child, releasing its pipes."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=_TERMINATE_GRACE)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=_TERMINATE_GRACE)
        except Exception as exc:  # pragma: no cover - defensive reap
            logger.warning("Codex App Server shutdown was unclean: %s", exc)
        finally:
            for stream in (proc.stdout, proc.stderr):
                if stream is not None and not stream.closed:
                    try:
                        stream.close()
                    except OSError:
                        pass
            for thread_attr in ("_stderr_thread", "_stdout_thread"):
                thread = getattr(self, thread_attr)
                if thread is not None:
                    thread.join(timeout=_TERMINATE_GRACE)
                    setattr(self, thread_attr, None)

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def __enter__(self) -> AppServerSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Protocol
    # ------------------------------------------------------------------

    def _stderr_hint(self) -> str:
        tail = "; ".join(list(self._stderr_tail)[-3:])
        return f" (stderr: {tail})" if tail else ""

    def _write(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise FetchError("Codex App Server is not running")
        try:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            raise FetchError(
                f"Codex App Server transport closed while sending "
                f"{message.get('method')!r}{self._stderr_hint()}"
            ) from exc

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._write({"method": method, "params": params or {}})

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send *method* and return its ``result``, correlating on request id.

        Buffers any other response or notification that arrives first — the
        live protocol genuinely reorders responses.
        """
        self._next_id += 1
        request_id = self._next_id
        self._write({"method": method, "id": request_id, "params": params or {}})
        message = self._await_response(request_id, self._timeout(timeout), method)

        error = message.get("error")
        if isinstance(error, dict):
            raise self._map_error(method, error)
        result = message.get("result")
        if not isinstance(result, dict):
            raise FetchError(f"Codex App Server {method!r} returned no result object")
        return result

    def wait_for_notification(self, method: str, timeout: float | None = None) -> dict[str, Any]:
        """Block until a notification named *method* arrives, and return it."""
        deadline = self._deadline(self._timeout(timeout))
        for index, buffered in enumerate(self._pending.notifications):
            if buffered.get("method") == method:
                del self._pending.notifications[index]
                return buffered
        while True:
            message = self._read_message(deadline, f"notification {method!r}")
            if message.get("method") == method and "id" not in message:
                return message
            self._buffer(message)

    def _timeout(self, timeout: float | None) -> float:
        return self._config.request_timeout if timeout is None else timeout

    @staticmethod
    def _deadline(timeout: float) -> float:
        # Monotonic: a wall-clock step (NTP, container resume) must not make a
        # request hang forever or expire instantly.
        return float("inf") if timeout <= 0 else time.monotonic() + timeout

    def _await_response(self, request_id: int, timeout: float, method: str) -> dict[str, Any]:
        buffered = self._pending.responses.pop(request_id, None)
        if buffered is not None:
            return buffered
        deadline = self._deadline(timeout)
        while True:
            message = self._read_message(deadline, f"request {method!r}")
            if message.get("id") == request_id:
                return message
            self._buffer(message)

    def _buffer(self, message: dict[str, Any]) -> None:
        message_id = message.get("id")
        if isinstance(message_id, int):
            self._pending.responses[message_id] = message
        elif message.get("method") is not None:
            self._pending.notifications.append(message)
        # Anything else is unrecognised framing; drop it rather than grow.

    def _read_message(self, deadline: float, what: str) -> dict[str, Any]:
        """Read one JSON message from the child, honouring *deadline*."""
        if self._proc is None:
            raise FetchError("Codex App Server is not running")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FetchError(f"Codex App Server timed out awaiting {what}")
            try:
                line = self._stdout_queue.get(
                    timeout=None if remaining == float("inf") else remaining
                )
            except queue.Empty:
                raise FetchError(
                    f"Codex App Server timed out awaiting {what}"
                ) from None
            if line is None:
                code = self._proc.poll()
                raise FetchError(
                    f"Codex App Server exited (code {code}) while awaiting "
                    f"{what}{self._stderr_hint()}"
                )
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                # Non-JSON chatter on stdout is not fatal on its own.
                logger.debug("Ignoring non-JSON line from Codex App Server")
                continue
            if isinstance(message, dict):
                return message

    @staticmethod
    def _map_error(method: str, error: dict[str, Any]) -> FetchError:
        message = error.get("message")
        text = message if isinstance(message, str) else "unknown error"
        # The App Server reports "authentication required" as a plain JSON-RPC
        # error rather than a typed code, so match on the message.
        if "authentication required" in text.lower() or "not authenticated" in text.lower():
            return CodexLoginRequired(
                f"Codex login required: {text} "
                "(run `usage-dashboard login codex` in the server pod)"
            )
        return FetchError(f"Codex App Server {method!r} failed: {text}")


class CodexAppServerClient:
    """Public App Server API, with a pluggable process-lifetime policy.

    ``persistent=True`` (the default) keeps one child and restarts it at most
    once per call if the transport breaks. This is the runtime policy: every
    *start* leaks ~29 kB into ``CODEX_HOME`` (uncheckpointed SQLite WALs and a
    temp directory), which is unbounded at one start per poll but irrelevant at
    one start per pod. See the module docstring for the measurements.

    ``persistent=False`` starts a child per call and closes it again. Correct
    for short-lived invocations — the enrolment CLI uses it so that its
    verification step runs against a genuinely fresh process. Both policies are
    exercised by the tests.
    """

    def __init__(
        self,
        config: CodexAppServerConfig | None = None,
        *,
        persistent: bool = True,
        session_factory: Callable[[CodexAppServerConfig], AppServerSession] | None = None,
    ) -> None:
        self._config = config or CodexAppServerConfig()
        self._persistent = persistent
        self._new_session = session_factory or AppServerSession
        # Held for a whole call in both policies: the scheduler loop and the
        # API-triggered fetch_now() path can overlap.
        self._lock = threading.RLock()
        self._session: AppServerSession | None = None
        self._closed = False

    @property
    def config(self) -> CodexAppServerConfig:
        return self._config

    def _fresh_session(self) -> AppServerSession:
        session = self._new_session(self._config)
        session.start()
        return session

    @contextmanager
    def _acquire(self) -> Iterator[AppServerSession]:
        """Yield a started session under the policy's lifetime rules."""
        with self._lock:
            if self._closed:
                raise FetchError("Codex App Server client is closed")
            if not self._persistent:
                session = self._fresh_session()
                try:
                    yield session
                finally:
                    session.close()
                return

            if self._session is not None and not self._session.alive:
                self._session.close()
                self._session = None
            if self._session is None:
                self._session = self._fresh_session()
            try:
                yield self._session
            except FetchError:
                # One restart per call, then let the scheduler's backoff own
                # the retry cadence rather than spinning here.
                broken, self._session = self._session, None
                if broken is not None:
                    broken.close()
                raise

    def close(self) -> None:
        """Release any retained child. Safe to call repeatedly."""
        with self._lock:
            self._closed = True
            session, self._session = self._session, None
        if session is not None:
            session.close()

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def read_account(self) -> dict[str, Any]:
        """Return ``account/read``: ``{"account": …|null, "requiresOpenaiAuth": bool}``."""
        with self._acquire() as session:
            return session.request("account/read")

    def read_rate_limits(self) -> dict[str, Any]:
        """Return ``account/rateLimits/read`` for the signed-in account."""
        with self._acquire() as session:
            return session.request("account/rateLimits/read")

    def read_account_and_rate_limits(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Both reads over a single child — the runtime fetch path.

        Two separate calls would spawn two children under the one-shot policy;
        the fetcher wants the auth check and the data from one process.
        """
        with self._acquire() as session:
            account = session.request("account/read")
            if not _has_account(account):
                raise CodexLoginRequired(
                    "Codex login required: no ChatGPT account is enrolled "
                    "(run `usage-dashboard login codex` in the server pod)"
                )
            return account, session.request("account/rateLimits/read")

    def device_code_login(
        self,
        present_code: Callable[[str, str], None],
        timeout: float = 900.0,
    ) -> None:
        """Run the one-time device-code enrolment.

        *present_code* receives ``(verification_url, user_code)`` and is the
        only thing shown to the operator — no token material is ever returned
        or printed. Blocks until Codex reports the login completed.
        """
        with self._acquire() as session:
            started = session.request(
                "account/login/start", {"type": "chatgptDeviceCode"}
            )
            login_id = started.get("loginId")
            url = started.get("verificationUrl")
            code = started.get("userCode")
            if not isinstance(url, str) or not isinstance(code, str):
                raise FetchError("Codex login/start returned no verification URL or code")
            present_code(url, code)
            try:
                session.wait_for_notification("account/login/completed", timeout=timeout)
            except BaseException:
                # Leave no half-open login behind for the next attempt.
                if isinstance(login_id, str):
                    try:
                        session.request(
                            "account/login/cancel", {"loginId": login_id}, timeout=10.0
                        )
                    except FetchError:
                        logger.debug("Codex login cancel failed; continuing")
                raise


def _has_account(account_read: dict[str, Any]) -> bool:
    """True when ``account/read`` shows an enrolled account.

    Verified live: a logged-out App Server answers ``{"account": null,
    "requiresOpenaiAuth": true}``.
    """
    return isinstance(account_read.get("account"), dict)
