"""Google Antigravity quota via the official ``agy`` CLI (Plan 007).

The dashboard never talks to Google's quota backend and never holds a Google
token. The official CLI owns the sign-in, the token file and its refresh; this
module runs its documented print-mode slash command and parses the structured
result::

    agy -p /usage --output-format json

The changelog documents that command as answering "without starting an agent
turn, spending quota, or leaving a conversation behind".

**Verified against agy 1.4.12 (linux x64, 2026-11-01), not docs.** Keep it
that way: re-verify every point below on a version bump.

- Result: ``command.data.groups[].buckets[]``, each with ``id``, ``window``
  (``"5h"``/``"weekly"``), ``remaining_fraction`` (0-1, *remaining*) and
  ``reset_time`` (UTC ``Z``). An AI Pro account shows a ``Gemini Models``
  group (``gemini-weekly``/``gemini-5h``), a ``Claude and GPT models`` group
  (``3p-*``) and a ``Gemini 4 Argon`` group whose only bucket is
  ``gemini-argon-wtu-weekly``. A live capture is in
  ``tests/fixtures/antigravity_usage_1.4.12.json``.
- ``--log-file /dev/null`` is load-bearing. By default every run writes a new
  ~34 kB ``log/cli-<ts>.log`` that is not pruned per run (~10 MB/day at a
  300 s poll, the same failure mode as the Codex WAL leak). A *fixed* log
  path is no better, because the CLI appends to it.
- ``AGY_CLI_DISABLE_AUTO_UPDATE`` must be ``true``. ``1`` is silently
  ignored, and print mode otherwise runs the updater.
- ``--disable-slash-commands=false`` guards a 1.4.9 bug where a saved
  ``disableSlashCommands`` setting turns ``/usage`` into a *model prompt*.
  The parser also refuses any result that spent a turn.
- Signed out, the CLI prints a Google sign-in URL on stderr and then waits
  60 s for a pasted code. ``--print-timeout`` does not end that wait, so a
  fetch kills the process as soon as the prompt appears and reports an auth
  failure rather than hanging the poll.
- The CLI keeps all state under ``$HOME/.gemini/``. The token is a file
  there (no keyring in a pod), so ``HOME`` alone selects the login.
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from usage_dashboard.server.fetch_types import FetchAuthError, FetchError, dump_json
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus, ScopedLimit

DEFAULT_AGY_BIN = "agy"
DEFAULT_ANTIGRAVITY_HOME = "/data/antigravity"

# A healthy run takes ~5 s. The bound only has to beat the poll interval and
# stop a wedged child from holding the scheduler thread.
_DEFAULT_TIMEOUT = 45.0

# What the signed-out CLI writes to stderr before it blocks on the paste
# prompt. Matching it lets a fetch fail fast instead of waiting out 60 s.
AUTH_REQUIRED_MARKER = "Authentication required"

# The only environment the child gets. The pod's environment carries the
# dashboard API key and provider secrets, which the CLI has no business
# seeing, and a stray GEMINI_API_KEY would silently switch it to API-key auth.
_ENV_PASSTHROUGH = (
    "PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
    # Egress proxies, for a cluster that routes through one.
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy",
)

ARGON_LABEL = "Argon"


@dataclass(frozen=True)
class AntigravityConfig:
    binary: str = DEFAULT_AGY_BIN
    home: str = DEFAULT_ANTIGRAVITY_HOME
    timeout: float = _DEFAULT_TIMEOUT


def usage_command(binary: str) -> list[str]:
    """The exact argv for the zero-turn usage query."""
    return [
        binary,
        "-p",
        "/usage",
        "--output-format",
        "json",
        "--disable-slash-commands=false",
        "--log-file",
        os.devnull,
    ]


def cli_env(home: str) -> dict[str, str]:
    """A minimal child environment that pins the CLI to *home*."""
    env = {name: os.environ[name] for name in _ENV_PASSTHROUGH if name in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["HOME"] = home
    env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    return env


def workspace_dir(home: str) -> str:
    """An empty cwd for the CLI, so it never treats some real tree as a project."""
    path = os.path.join(home, "workspace")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# One owner of the home directory
#
# Enrolment signs in inside a staging directory and then swaps it into place.
# The fetcher and the swap share an advisory lock so a poll never runs against
# a half-moved directory. It is a *file* lock because the `kubectl exec`
# enrolment path runs in a second process. The lock file sits beside the home
# rather than inside it, since the home itself is what gets replaced.
# ---------------------------------------------------------------------------


@contextmanager
def home_lock(home: str) -> Iterator[None]:
    parent = os.path.dirname(os.path.abspath(home))
    os.makedirs(parent, exist_ok=True)
    fd = os.open(f"{os.path.abspath(home)}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _prev_dir(home: str) -> str:
    return f"{os.path.abspath(home)}.prev"


def _swap_marker(home: str) -> str:
    return f"{os.path.abspath(home)}.swapping"


def _recover_interrupted_swap(home: str) -> None:
    """Settle a swap that died part-way (caller holds the lock).

    Recovery keys on the marker, never on a missing home alone: deleting
    ``home`` is how an operator signs out, and that must not resurrect the
    old login from a leftover ``.prev``.
    """
    home = os.path.abspath(home)
    prev = _prev_dir(home)
    marker = _swap_marker(home)
    if os.path.exists(marker):
        if not os.path.exists(home) and os.path.isdir(prev):
            # Died between the two renames: the old login is the only one.
            os.rename(prev, home)
        os.unlink(marker)
    if os.path.exists(prev):
        # Died after the swap completed, before the old login was removed.
        shutil.rmtree(prev, ignore_errors=True)


def make_staging_home(home: str) -> str:
    """A fresh 0700 sibling of *home* for a sign-in ceremony.

    It has to be a sibling so that the final swap is a same-filesystem rename.
    """
    parent = os.path.dirname(os.path.abspath(home))
    os.makedirs(parent, exist_ok=True)
    prefix = os.path.basename(os.path.abspath(home)) + ".enrol-"
    _sweep_stale_staging(parent, prefix)
    return tempfile.mkdtemp(prefix=prefix, dir=parent)


# A staging dir older than this belongs to a ceremony that was killed outright
# (the CLI's own finally removes it otherwise). The login window is 60 s, so
# an hour leaves a wide margin for one that is still running.
_STALE_STAGING_SECONDS = 3600.0


def _sweep_stale_staging(parent: str, prefix: str) -> None:
    cutoff = time.time() - _STALE_STAGING_SECONDS
    try:
        entries = os.listdir(parent)
    except OSError:
        return
    for name in entries:
        path = os.path.join(parent, name)
        if not name.startswith(prefix) or not os.path.isdir(path):
            continue
        try:
            if os.stat(path).st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def install_home(staging: str, home: str) -> None:
    """Make a verified staging directory the live home.

    The previous home survives as ``<home>.prev`` until the new one is in
    place, so a crash mid-swap is undone by the next fetch, not lost.
    """
    home = os.path.abspath(home)
    prev = _prev_dir(home)
    marker = _swap_marker(home)
    with home_lock(home):
        _recover_interrupted_swap(home)
        had_home = os.path.exists(home)
        if had_home:
            with open(marker, "w"):
                pass
            os.rename(home, prev)
        os.rename(staging, home)
        if had_home:
            os.unlink(marker)
            shutil.rmtree(prev, ignore_errors=True)


# ---------------------------------------------------------------------------
# Running the CLI
# ---------------------------------------------------------------------------


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pass


def run_usage_command(config: AntigravityConfig) -> dict[str, Any]:
    """Run the usage query against ``config.home`` and return its JSON result.

    Raises FetchAuthError when the CLI is signed out (detected from the sign-in
    prompt, without waiting for its 60 s window), and FetchError on anything
    else that is not a JSON object.
    """
    with home_lock(config.home):
        _recover_interrupted_swap(config.home)
        os.makedirs(config.home, mode=0o700, exist_ok=True)
        cwd = workspace_dir(config.home)
        try:
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                usage_command(config.binary),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=cli_env(config.home),
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            raise FetchError(
                f"Antigravity CLI could not start ({config.binary!r}): {exc}"
            ) from exc

        stdout_chunks: list[bytes] = []
        auth_required = threading.Event()
        assert proc.stdout is not None and proc.stderr is not None
        stdout, stderr = proc.stdout, proc.stderr

        def _drain_stdout() -> None:
            while chunk := stdout.read(65536):
                stdout_chunks.append(chunk)

        def _watch_stderr() -> None:
            # Never logged: when signed out this carries a sign-in URL. Only
            # the marker's presence is used.
            marker = AUTH_REQUIRED_MARKER.encode()
            for line in iter(stderr.readline, b""):
                if marker in line:
                    auth_required.set()

        readers = [
            threading.Thread(target=_drain_stdout, daemon=True),
            threading.Thread(target=_watch_stderr, daemon=True),
        ]
        for reader in readers:
            reader.start()

        deadline = time.monotonic() + config.timeout
        timed_out = False
        while proc.poll() is None:
            if auth_required.is_set():
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.05)
        if proc.poll() is None:
            _kill_group(proc)
        for reader in readers:
            reader.join(timeout=2.0)

    if auth_required.is_set():
        raise FetchAuthError("Antigravity CLI is signed out")
    if timed_out:
        raise FetchError(f"Antigravity CLI timed out after {config.timeout:.0f}s")
    raw = b"".join(stdout_chunks).decode("utf-8", "replace")
    if not raw.strip():
        raise FetchError(f"Antigravity CLI exited {proc.returncode} with no output")
    return parse_result_text(raw)


def parse_result_text(raw: str) -> dict[str, Any]:
    """The print-mode JSON result from the CLI's stdout.

    A signed-in run prints exactly one JSON object. A run that signed in
    first has not been observed end to end, so tolerate any chatter before
    the result by falling back to the last line that is a JSON object.
    """
    text = raw.strip()
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            break
        if payload is None:
            raise FetchError("Antigravity CLI output is not JSON") from None
    if not isinstance(payload, dict):
        raise FetchError("Antigravity CLI output is not a JSON object")
    return payload


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_reset(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _used_percent(bucket: dict[str, Any]) -> float | None:
    """Percent *used* (the dashboard convention), from a remaining fraction.

    A ``disabled`` bucket cannot serve requests, so it reads as exhausted
    rather than as the 100% remaining it may still report. Proto3 JSON drops
    a zero ``remaining_fraction``, so a bucket that has a reset time but no
    fraction is exhausted too. With neither, the value is unknown.
    """
    if bucket.get("disabled") is True:
        return 100.0
    fraction = bucket.get("remaining_fraction")
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
        return 100.0 if _parse_reset(bucket.get("reset_time")) is not None else None
    if fraction != fraction:  # NaN
        return None
    remaining = min(max(float(fraction), 0.0), 1.0)
    return round((1.0 - remaining) * 100.0, 2)


# The shared Flash/Pro pool's bucket ids as agy 1.4.12 reports them.
_GEMINI_POOL_IDS = frozenset({"gemini-5h", "gemini-weekly"})


def _is_argon(group_name: str, bucket_id: str) -> bool:
    return "argon" in group_name.lower() or "argon" in bucket_id.lower()


def _is_gemini_pool(group_name: str, bucket_id: str) -> bool:
    """The shared Flash/Pro pool. Matched on name *or* id, because the
    ``-wtu-`` style infixes look experiment-flavoured and may shift."""
    return group_name.lower().startswith("gemini") or bucket_id.lower().startswith("gemini-")


def check_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the print-mode envelope and return ``command.data``.

    Raises FetchAuthError for a signed-out result and FetchError for anything
    else that is not a clean, zero-turn ``/usage`` answer.
    """
    status = payload.get("status")
    if status != "SUCCESS":
        error = str(payload.get("error") or status or "unknown error")
        if "authentication" in error.lower():
            raise FetchAuthError(f"Antigravity CLI: {error}")
        raise FetchError(f"Antigravity CLI reported {status}: {error[:200]}")
    turns = payload.get("num_turns")
    if isinstance(turns, int) and not isinstance(turns, bool) and turns != 0:
        # The disableSlashCommands bug: /usage went to the model as a prompt.
        raise FetchError(
            f"Antigravity CLI answered /usage with {turns} model turn(s) instead "
            "of the slash command; refusing the result"
        )
    command = payload.get("command")
    if not isinstance(command, dict) or command.get("name") != "usage":
        raise FetchError("Antigravity CLI result is not a /usage answer")
    data = command.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        raise FetchError("Antigravity /usage result has no quota groups")
    return data


def parse_usage(payload: dict[str, Any]) -> Reading:
    """Build a Reading from the CLI's ``/usage`` JSON result.

    The Gemini pool (Flash/Pro) fills the session (5h) and weekly windows.
    Argon, which has its own weekly-only bucket on Pro plans, becomes a scoped
    limit, the same shape as Claude's Fable window. The Claude/GPT bucket
    Antigravity also reports is not this dashboard's concern (Claude and Codex
    have their own tiles) and is ignored.
    """
    data = check_result(payload)

    # The pool's documented ids win; the name/prefix heuristics only fill a
    # window no exact id did, so a future "Gemini <something>" group listed
    # first cannot quietly take over the Flash/Pro bars.
    exact: dict[str, tuple[float | None, datetime | None]] = {}
    session: tuple[float | None, datetime | None] = (None, None)
    weekly: tuple[float | None, datetime | None] = (None, None)
    argon: ScopedLimit | None = None
    for group in data["groups"]:
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("name") or "")
        buckets = group.get("buckets")
        if not isinstance(buckets, list):
            continue
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            bucket_id = str(bucket.get("id") or "")
            window = bucket.get("window")
            percent = _used_percent(bucket)
            reset = _parse_reset(bucket.get("reset_time"))
            if percent == 0.0:
                # An untouched bucket reports "now + window" as its reset, so
                # it slides forward on every poll (verified: 09:11 -> 10:45
                # between two polls of the same full 5h bucket). Nothing is
                # actually pending, and a sliding reset would read as movement
                # to the scheduler and pin the idle ladder to its floor.
                reset = None
            if bucket_id in _GEMINI_POOL_IDS:
                exact[bucket_id] = (percent, reset)
            elif _is_argon(group_name, bucket_id):
                if window == "weekly" and argon is None:
                    argon = ScopedLimit(name=ARGON_LABEL, percent=percent, resets_at=reset)
            elif _is_gemini_pool(group_name, bucket_id):
                if window == "5h" and session == (None, None):
                    session = (percent, reset)
                elif window == "weekly" and weekly == (None, None):
                    weekly = (percent, reset)

    session = exact.get("gemini-5h", session)
    weekly = exact.get("gemini-weekly", weekly)

    if session[0] is None and weekly[0] is None and (argon is None or argon.percent is None):
        # Loud, not an empty tile: a schema change has to be noticed.
        raise FetchError("Antigravity /usage result has no Gemini quota buckets")

    return Reading(
        provider=Provider.ANTIGRAVITY,
        status=ReadingStatus.CURRENT,
        session_percent=session[0],
        session_resets_at=session[1],
        weekly_percent=weekly[0],
        weekly_resets_at=weekly[1],
        fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
        stale=False,
        scoped_limits=[argon] if argon is not None else None,
    )


def fetch_antigravity_usage(config: AntigravityConfig) -> Reading:
    payload = run_usage_command(config)
    dump_json("antigravity_usage_raw.json", payload)
    return parse_usage(payload)
