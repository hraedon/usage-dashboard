"""Claude credentials owned by the official Claude Code CLI (Plan 009).

The Codex/Antigravity ownership model, applied to Claude. Each account has a
persistent ``CLAUDE_CONFIG_DIR`` on the PVC (``/data/claude-personal``,
``/data/claude-work``). The official CLI signs in there and is the **only**
refresher of that credential family: the dashboard never POSTs to a token
endpoint. What the dashboard still does itself is the usage *read*: it takes
the access token out of the CLI's ``.credentials.json`` (via the quarantined
parser in :mod:`usage_dashboard.claude_credentials`) and calls the
undocumented ``GET /api/oauth/usage``, exactly as before.

Facts below were **verified against Claude Code 2.1.276 in the server pod,
not docs** (Plan 009 WP-1, 2026-11-08). Keep it that way:

- ``claude auth status --json`` is NOT a refresh trigger. It is a local read
  (~0.15 s), never rewrites the file, and reports ``loggedIn: true`` even
  for an expired access token.
- ``claude -p /usage --output-format json`` IS one. It is a zero-turn local
  command (``num_turns == 0``, ``total_cost_usd == 0``, ``local_command ==
  "usage"``, ~1 s) that calls the API, so with an expired access token the CLI
  refreshes first. It rotates the refresh token and rewrites
  ``.credentials.json`` atomically (new inode: temp file + rename), adding
  ``refreshTokenExpiresAt`` and ``rateLimitTier``.
- Signed out (no credential file) it exits 0 in ~40 ms with
  ``is_error: false`` and a session-cost summary instead of usage. So the
  trigger's output is never trusted as a verdict: success is judged by
  re-reading the credential file afterwards.
- ``--no-session-persistence`` stops a ~3 kB transcript per run under
  ``projects/``. ``HOME`` gains ~6 kB of cache per run, so every run gets a
  throwaway ``HOME``; the config dir itself holds the credential.
- ``--bare`` is unusable: it disables OAuth entirely.
  ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`` breaks ``/usage``.
- The first run downloads ~4.3 MB (bundled skills) into the config dir, once.

The trigger runs only when the access token is at or near expiry, or after the
usage endpoint rejects it — a few times a day per account, not per poll.

POSIX-only (``fcntl`` via :mod:`cli_home`), like the rest of the server.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Any

from usage_dashboard.claude_credentials import (
    ClaudeCredential,
    ClaudeCredentialError,
    parse_credentials,
)
from usage_dashboard.server.cli_home import home_lock, recover_interrupted_swap
from usage_dashboard.server.fetch_claude import fetch_claude_usage
from usage_dashboard.server.fetch_types import FetchAuthError, FetchError
from usage_dashboard.shared.models import Provider, Reading

logger = logging.getLogger(__name__)

DEFAULT_CLAUDE_BIN = "claude"
CREDENTIALS_FILENAME = ".credentials.json"

# Token-store-era account keys -> where each account's CLI profile lives. The
# env overrides exist for tests and non-default PVC layouts.
DEFAULT_PROFILE_DIRS = {
    "claude": "/data/claude-personal",
    "claude_work": "/data/claude-work",
}
PROFILE_DIR_ENV = {
    "claude": "CLAUDE_CONFIG_DIR_PERSONAL",
    "claude_work": "CLAUDE_CONFIG_DIR_WORK",
}
ACCOUNT_PROVIDERS = {"claude": Provider.CLAUDE, "claude_work": Provider.CLAUDE_WORK}

# Run the trigger once the access token is this close to expiry. The CLI has
# its own (smaller or equal) buffer; if it declines to refresh a token that is
# still valid, the fetch simply proceeds with it and the next poll tries again.
REFRESH_SKEW_SECONDS = 300

# A healthy run takes ~1 s; the bound only has to stop a wedged child from
# holding the scheduler thread (and the profile lock) indefinitely.
_DEFAULT_TIMEOUT = 60.0

# The only environment the child gets: the pod's environment carries the
# dashboard API key and every provider secret, and a stray ANTHROPIC_API_KEY
# would silently switch the CLI to API-key auth.
_ENV_PASSTHROUGH = (
    "PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy",
)

# A torn read is unlikely (the CLI writes atomically, verified) but cheap to
# absorb: one short retry before a parse failure is reported.
_TORN_READ_RETRY_SECONDS = 0.2


@dataclass(frozen=True)
class ClaudeProfileConfig:
    """One Claude account's CLI profile."""

    config_dir: str
    binary: str = DEFAULT_CLAUDE_BIN
    timeout: float = _DEFAULT_TIMEOUT


def profile_dir(store_key: str) -> str:
    """The configured ``CLAUDE_CONFIG_DIR`` for an account key."""
    return os.environ.get(PROFILE_DIR_ENV[store_key]) or DEFAULT_PROFILE_DIRS[store_key]


def credentials_path(config_dir: str) -> str:
    return os.path.join(config_dir, CREDENTIALS_FILENAME)


def is_enrolled(config_dir: str) -> bool:
    """True when the profile holds a CLI credential file (signed in)."""
    return os.path.isfile(credentials_path(config_dir))


def refresh_command(binary: str) -> list[str]:
    """The exact argv for the zero-turn refresh trigger."""
    return [
        binary,
        "-p",
        "/usage",
        "--output-format",
        "json",
        "--no-session-persistence",
    ]


def cli_env(config_dir: str, home: str) -> dict[str, str]:
    """A minimal child environment pinning the CLI to *config_dir*."""
    env = {name: os.environ[name] for name in _ENV_PASSTHROUGH if name in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["HOME"] = home
    env["CLAUDE_CONFIG_DIR"] = config_dir
    env["DISABLE_AUTOUPDATER"] = "1"
    return env


def read_credential(config_dir: str) -> ClaudeCredential:
    """Parse the profile's credential file.

    Raises FetchAuthError when there is none (signed out: re-enrol), and
    FetchError when it is unreadable or of an unknown shape — a re-login would
    not fix schema drift, so that must not look like an auth failure.
    """
    path = credentials_path(config_dir)
    for attempt in (1, 2):
        try:
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
            break
        except FileNotFoundError as exc:
            raise FetchAuthError(
                "Claude is not signed in (no CLI credential in "
                f"{config_dir}); enrol it from the /login pane"
            ) from exc
        except (OSError, ValueError) as exc:
            if attempt == 2:
                raise FetchError(
                    f"Claude CLI credential unreadable: {type(exc).__name__}"
                ) from exc
            time.sleep(_TORN_READ_RETRY_SECONDS)
    try:
        return parse_credentials(raw)
    except ClaudeCredentialError as exc:
        raise FetchError(str(exc)) from exc


def _near_expiry(credential: ClaudeCredential, now: float | None = None) -> bool:
    if credential.expires_at is None:
        # No expiry recorded: rely on the 401 path.
        return False
    now_ms = (time.time() if now is None else now) * 1000
    return credential.expires_at - now_ms <= REFRESH_SKEW_SECONDS * 1000


def _expired(credential: ClaudeCredential, now: float | None = None) -> bool:
    if credential.expires_at is None:
        return False
    now_ms = (time.time() if now is None else now) * 1000
    return credential.expires_at <= now_ms


# On a timeout the child gets SIGTERM and this long to exit before SIGKILL. A
# CLI killed outright between rotating the token server-side and renaming the
# new file into place would strand the profile on a dead refresh token.
_KILL_GRACE_SECONDS = 5.0

# Words in an ``is_error`` result that mean the CLI's own auth is broken (a
# re-login fixes it), as opposed to a transient failure (a retry fixes it).
_AUTH_HINTS = ("login", "log in", "auth", "expired", "invalid_grant", "401", "403")


def _stop_group(proc: subprocess.Popen[bytes]) -> None:
    for sig, grace in ((signal.SIGTERM, _KILL_GRACE_SECONDS), (signal.SIGKILL, None)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            if sig == signal.SIGKILL:
                proc.kill()
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def run_refresh_trigger(config: ClaudeProfileConfig) -> dict[str, Any]:
    """Run the zero-turn ``/usage`` command so the CLI refreshes if it must.

    The caller holds the profile lock. Whether a refresh happened is judged by
    re-reading the credential file, never from this output (a signed-out CLI
    also "succeeds"). Raises FetchAuthError only when the CLI itself reports an
    auth problem, and plain FetchError for anything transient or malformed.
    Neither the child's stderr nor its result text is ever logged.
    """
    with tempfile.TemporaryDirectory(prefix="claude-cli-home-") as home:
        try:
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                refresh_command(config.binary),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=home,
                env=cli_env(config.config_dir, home),
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            raise FetchError(
                f"Claude CLI could not start ({config.binary!r}): {exc}"
            ) from exc
        try:
            out, _ = proc.communicate(timeout=config.timeout)
        except subprocess.TimeoutExpired as exc:
            _stop_group(proc)
            raise FetchError(
                f"Claude CLI refresh trigger timed out after {config.timeout:.0f}s"
            ) from exc
    try:
        payload = json.loads(out.decode("utf-8", "replace"))
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or payload.get("type") != "result":
        raise FetchError(
            f"Claude CLI refresh trigger exited {proc.returncode} without a JSON result"
        )
    if payload.get("num_turns") != 0:
        # Something in the profile turned `/usage` into a model prompt. That
        # costs quota on every refresh, so refuse loudly rather than continue.
        raise FetchError(
            "Claude CLI refresh trigger ran a model turn instead of the local "
            "/usage command; check the profile's settings"
        )
    if payload.get("is_error"):
        text = str(payload.get("result", "")).lower()
        if any(hint in text for hint in _AUTH_HINTS):
            raise FetchAuthError("Claude CLI reports its login is no longer valid")
        raise FetchError("Claude CLI refresh trigger reported an error")
    return payload


def fetch_claude_profile_usage(
    config: ClaudeProfileConfig, provider: Provider = Provider.CLAUDE
) -> Reading:
    """Read one account's usage with the CLI-owned credential.

    Holds the profile lock throughout, so neither an enrolment swap nor a
    concurrent fetch (``POST /refresh`` vs the poll loop) can interleave with
    a refresh.

    Failure classes matter here, because an auth failure backs the tile off to
    the cap behind a "login required" prompt: only a *clean* trigger run that
    leaves the token expired, or the CLI itself reporting an auth problem, is
    an auth failure. A trigger that times out or misbehaves is transient, and
    a token that has not actually expired yet is still used.
    """
    with home_lock(config.config_dir):
        recover_interrupted_swap(config.config_dir)
        credential = read_credential(config.config_dir)
        if _near_expiry(credential):
            try:
                run_refresh_trigger(config)
            except FetchAuthError:
                raise
            except FetchError as exc:
                if _expired(credential):
                    raise
                logger.warning(
                    "Claude CLI refresh trigger failed for %s (%s); the access "
                    "token has not expired yet, so it is still used",
                    provider.value,
                    exc,
                )
            else:
                refreshed = read_credential(config.config_dir)
                if refreshed.access_token != credential.access_token:
                    logger.info("Claude CLI refreshed the %s credential", provider.value)
                elif _expired(refreshed):
                    raise FetchAuthError(
                        "Claude access token expired and the CLI did not refresh "
                        "it; re-enrol from the /login pane"
                    )
                credential = refreshed
        try:
            return fetch_claude_usage(credential.access_token, provider)
        except FetchAuthError as rejected:
            # Rejected before its recorded expiry (clock skew, early
            # revocation). Give the CLI one chance; it owns the refresh.
            try:
                run_refresh_trigger(config)
            except FetchAuthError:
                raise
            except FetchError as exc:
                # Unknown whether a re-login is needed: back off as transient
                # rather than parking the tile behind "login required".
                raise FetchError(
                    f"Claude rejected the access token and the CLI refresh "
                    f"trigger failed: {exc}"
                ) from rejected
            retried = read_credential(config.config_dir)
            if retried.access_token == credential.access_token:
                raise
            logger.info(
                "Claude CLI refreshed the %s credential after a rejection",
                provider.value,
            )
            return fetch_claude_usage(retried.access_token, provider)
