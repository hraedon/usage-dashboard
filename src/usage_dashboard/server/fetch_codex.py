"""Fetch OpenAI Codex (ChatGPT-plan) usage.

Mirrors the Claude OAuth pattern: a dedicated ("sacrificial") ChatGPT login
mints an access/refresh token pair, the scheduler refreshes it when it 401s,
and this module reads the rolling rate-limit windows.

Endpoints and shapes were taken from the ``openai/codex`` CLI source (the same
approach Plan 001 used for Claude — don't trust them from memory):

- Token refresh: ``POST https://auth.openai.com/oauth/token`` (public PKCE
  client ``app_EMoamEEZ73f0CkXaXp7hrann``), grant_type ``refresh_token``.
- Usage: ``GET https://chatgpt.com/backend-api/wham/usage`` (sent with the
  ``chatgpt-account-id`` header) returns a ``rate_limit`` object with
  ``primary_window`` (~5h session) and ``secondary_window`` (weekly), each
  ``{used_percent, limit_window_seconds, reset_after_seconds, reset_at}``.
  ``reset_at`` is an absolute Unix epoch in **seconds**. This live shape was
  captured from a real ChatGPT-plan account (2026-07-10); it differs from the
  streaming/header struct in the source (``rate_limits.{primary,secondary}``
  with ``resets_at``), which the parser also tolerates as a fallback.

**Weekly-only mode:** OpenAI periodically drops the session (5h) window,
leaving only the weekly limit. When only one window is present, the parser
classifies it by ``limit_window_seconds`` (weekly ≈ 604 800 s; session ≈
18 000 s) rather than by slot name, so a lone weekly window in the
``primary_window`` slot is still mapped to ``weekly_percent``. Both formats
(session+weekly and weekly-only) are handled transparently.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from usage_dashboard.server.fetch_types import (
    FetchAuthError,
    FetchError,
    FetchRateLimitError,
    dump_json,
)
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus

_CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_CODEX_SCOPES = "openid profile email offline_access"
# Mirror the CLI's originator/UA so the request looks like a Codex client.
_CODEX_ORIGINATOR = "codex_cli_rs"
_USER_AGENT = "usage-dashboard-codex/1.0"
_TIMEOUT = 30.0

# Windows with limit_window_seconds at or above this are weekly, not session.
# Session ≈ 18 000 s (5 h); weekly ≈ 604 800 s (7 d) — any threshold well
# above 18 000 but well below 604 800 works.
_WEEKLY_MIN_SECONDS = 100_000

logger = logging.getLogger(__name__)


def _epoch_to_naive_utc(value: object) -> datetime | None:
    """Convert an absolute Unix epoch (seconds) to a naive-UTC datetime.

    ``resets_at`` is an absolute timestamp in the Codex API; a missing/invalid
    value yields None (the model and renderers already handle "unknown").
    """
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)


def _extract_window(block: object) -> tuple[float | None, datetime | None]:
    """Pull (used_percent, reset_at) from a rate-limit window block.

    The live GET ``/wham/usage`` window uses ``reset_at`` (absolute epoch
    seconds); the streaming/header struct variant uses ``resets_at`` — accept
    either.
    """
    if not isinstance(block, dict):
        return None, None
    pct = block.get("used_percent")
    percent = float(pct) if isinstance(pct, (int, float)) else None
    reset = block.get("reset_at")
    if reset is None:
        reset = block.get("resets_at")
    return percent, _epoch_to_naive_utc(reset)


def _window_seconds(block: object) -> float | None:
    """Pull ``limit_window_seconds`` from a rate-limit window block."""
    if not isinstance(block, dict):
        return None
    seconds = block.get("limit_window_seconds")
    return float(seconds) if isinstance(seconds, (int, float)) else None


def _rate_limit_object(data: dict[str, Any]) -> dict[str, Any] | None:
    """Return the rate-limit object carrying the primary/secondary windows.

    The live GET endpoint nests them under ``rate_limit`` (singular). Fall
    back to the ``rate_limits`` object/list shape from the openai/codex structs
    so either representation parses.
    """
    rl = data.get("rate_limit")
    if isinstance(rl, dict):
        return rl
    alt = data.get("rate_limits")
    if isinstance(alt, dict):
        return alt
    if isinstance(alt, list):
        for item in alt:
            if isinstance(item, dict):
                return item
    return None


def _window(block: dict[str, Any], *names: str) -> object:
    """First present window sub-object among *names* (e.g. primary_window)."""
    for name in names:
        win = block.get(name)
        if isinstance(win, dict):
            return win
    return None


def refresh_codex_token(
    refresh_token: str,
    client_id: str | None = None,
) -> tuple[str, str]:
    """Refresh a Codex access token, returning (access, refresh).

    The token endpoint rotates the refresh token; fall back to the old one if
    a new one isn't returned (mirrors ``refresh_claude_token``).
    """
    payload: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id or _CODEX_CLIENT_ID,
        "scope": _CODEX_SCOPES,
    }
    try:
        # OpenAI's /oauth/token expects application/x-www-form-urlencoded
        # (confirmed against the openai/codex source), not JSON.
        response = httpx.post(_CODEX_TOKEN_URL, data=payload, timeout=_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        new_access = data["access_token"]
        new_refresh = data.get("refresh_token", refresh_token)
        return new_access, new_refresh
    except httpx.HTTPError as exc:
        raise FetchError(
            f"Codex token refresh failed: {type(exc).__name__}"
        ) from exc
    except (KeyError, ValueError, TypeError) as exc:
        raise FetchError(
            f"Codex token refresh parse error: {type(exc).__name__}"
        ) from exc


def fetch_codex_usage(
    access_token: str,
    account_id: str | None = None,
) -> Reading:
    """Fetch Codex usage as a Reading (primary→session, secondary→weekly).

    ``account_id`` (the ChatGPT ``chatgpt-account-id``) is sent when known;
    ChatGPT-plan accounts generally require it to resolve the right workspace.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "originator": _CODEX_ORIGINATOR,
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    }
    if account_id:
        headers["chatgpt-account-id"] = account_id
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.get(_CODEX_USAGE_URL, headers=headers)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 401:
            raise FetchAuthError("Codex usage request rejected: HTTP 401") from exc
        if status == 403:
            raise FetchError("Codex usage request forbidden: HTTP 403") from exc
        if status == 429:
            retry_after: float | None = None
            header = exc.response.headers.get("retry-after")
            if header is not None and header.isdigit():
                retry_after = float(header)
            raise FetchRateLimitError(
                f"Codex usage request rate limited: HTTP 429 (retry-after {header})",
                retry_after_seconds=retry_after,
            ) from exc
        raise FetchError(f"Codex usage request failed: HTTP {status}") from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"Codex usage request failed: {type(exc).__name__}") from exc

    dump_json("codex_raw.json", data)

    if not isinstance(data, dict):
        raise FetchError("Codex usage response parse error: not an object")
    rl = _rate_limit_object(data)
    if rl is None:
        raise FetchError("Codex usage response parse error: no rate_limit")
    try:
        primary_block = _window(rl, "primary_window", "primary")
        secondary_block = _window(rl, "secondary_window", "secondary")
        session_percent, session_resets_at = _extract_window(primary_block)
        weekly_percent, weekly_resets_at = _extract_window(secondary_block)
    except (ValueError, TypeError) as exc:
        raise FetchError(f"Codex usage response parse error: {type(exc).__name__}") from exc

    # Weekly-only mode: OpenAI may drop the session (primary) window, leaving
    # only the weekly limit. When only one window is present, classify it by
    # limit_window_seconds rather than slot name, so a weekly window in the
    # primary slot is still mapped to weekly_percent.
    if weekly_percent is None and session_percent is not None:
        seconds = _window_seconds(primary_block)
        if seconds is not None and seconds >= _WEEKLY_MIN_SECONDS:
            weekly_percent, weekly_resets_at = session_percent, session_resets_at
            session_percent, session_resets_at = None, None
    elif session_percent is None and weekly_percent is not None:
        seconds = _window_seconds(secondary_block)
        if seconds is not None and seconds < _WEEKLY_MIN_SECONDS:
            session_percent, session_resets_at = weekly_percent, weekly_resets_at
            weekly_percent, weekly_resets_at = None, None

    return Reading(
        provider=Provider.CODEX,
        status=ReadingStatus.CURRENT,
        session_percent=session_percent,
        session_resets_at=session_resets_at,
        weekly_percent=weekly_percent,
        weekly_resets_at=weekly_resets_at,
        fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
        stale=False,
    )
