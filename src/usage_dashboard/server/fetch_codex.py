"""Parse OpenAI Codex (ChatGPT-plan) usage from the official App Server.

Plan 005 moved this provider off the dashboard's own ChatGPT OAuth: the
`codex` CLI now owns the login, token storage and refresh, and this module is
a pure adapter over ``account/rateLimits/read``. It holds **no** OpenAI client
id, token endpoint, ``codex_cli_rs`` originator or direct HTTP call — see
``codex_app_server.py`` for the transport.

Response shape (captured live from codex-cli 0.155.1, not from docs)::

    {"rateLimits":            {"limitId": "codex", "primary": {...}, ...},
     "rateLimitsByLimitId":   {"codex":                {"primary": {...}, ...},
                               "base_model_inference": {"primary": {...}, ...}},
     "accountId": "...", ...}

Each window is ``{usedPercent, windowDurationMins, resetsAt}``, where
``resetsAt`` is an absolute Unix epoch in **seconds**.

Two things that bite:

**Bucket choice.** ``rateLimitsByLimitId["codex"]`` is preferred over the
compatibility ``rateLimits`` object. A live account carries a second
``base_model_inference`` bucket alongside it; folding that into the Codex tile
would silently mix two different limits, so unknown buckets are ignored.

**Units.** ``windowDurationMins`` is in MINUTES — the pre-migration endpoint
reported ``limit_window_seconds``. Session ≈ 300 min (5 h), weekly = 10 080 min
(7 d). Reusing the old seconds threshold (100 000) here would classify *every*
weekly window as a session window, because 10 080 < 100 000. The threshold
below is named in minutes for that reason, and the weekly-only case is not
hypothetical: a live Pro account currently reports the weekly window in the
``primary`` slot with ``secondary: null``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from usage_dashboard.server.codex_app_server import CodexAppServerClient
from usage_dashboard.server.fetch_types import FetchError, dump_json
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus

# The bucket that backs the Codex tile. Other buckets in rateLimitsByLimitId
# (e.g. base_model_inference) are deliberately ignored.
_CODEX_LIMIT_ID = "codex"

# Windows at or above this duration are weekly, not session. MINUTES, not
# seconds: session ≈ 300, weekly = 10 080; any threshold well above 300 and
# well below 10 080 works.
_WEEKLY_MIN_MINUTES = 1_440.0

logger = logging.getLogger(__name__)


def _epoch_to_naive_utc(value: object) -> datetime | None:
    """Convert an absolute Unix epoch (seconds) to a naive-UTC datetime.

    A missing/invalid value yields None (the model and renderers already
    handle "unknown"). ``bool`` is excluded because it is an ``int`` subclass
    and a stray ``True`` would otherwise become 1970-01-01.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _extract_window(block: object) -> tuple[float | None, datetime | None]:
    """Pull (usedPercent, resetsAt) from one rate-limit window."""
    if not isinstance(block, dict):
        return None, None
    pct = block.get("usedPercent")
    percent = float(pct) if isinstance(pct, (int, float)) and not isinstance(pct, bool) else None
    return percent, _epoch_to_naive_utc(block.get("resetsAt"))


def _window_minutes(block: object) -> float | None:
    """Pull ``windowDurationMins`` from one rate-limit window."""
    if not isinstance(block, dict):
        return None
    minutes = block.get("windowDurationMins")
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
        return None
    return float(minutes)


def _select_bucket(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the Codex limit bucket, preferring the per-limit-id map.

    ``rateLimitsByLimitId["codex"]`` is authoritative; ``rateLimits`` is the
    compatibility shape for responses that lack the map.
    """
    by_id = payload.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        bucket = by_id.get(_CODEX_LIMIT_ID)
        if isinstance(bucket, dict):
            return bucket
    bucket = payload.get("rateLimits")
    if isinstance(bucket, dict):
        return bucket
    raise FetchError("Codex rate-limit response parse error: no codex limit bucket")


def parse_rate_limits(payload: dict[str, Any]) -> Reading:
    """Build a Reading from an ``account/rateLimits/read`` result."""
    if not isinstance(payload, dict):
        raise FetchError("Codex rate-limit response parse error: not an object")

    bucket = _select_bucket(payload)
    primary = bucket.get("primary")
    secondary = bucket.get("secondary")

    session_percent, session_resets_at = _extract_window(primary)
    weekly_percent, weekly_resets_at = _extract_window(secondary)

    # Only one window present: classify it by duration rather than by which
    # slot it arrived in. A lone weekly window in the `primary` slot is the
    # live shape for a Pro account, so getting this wrong is not a corner case.
    if weekly_percent is None and session_percent is not None:
        minutes = _window_minutes(primary)
        if minutes is not None and minutes >= _WEEKLY_MIN_MINUTES:
            weekly_percent, weekly_resets_at = session_percent, session_resets_at
            session_percent, session_resets_at = None, None
    elif session_percent is None and weekly_percent is not None:
        minutes = _window_minutes(secondary)
        if minutes is not None and minutes < _WEEKLY_MIN_MINUTES:
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


def fetch_codex_usage(client: CodexAppServerClient) -> Reading:
    """Fetch Codex usage through the App Server.

    Raises :class:`CodexLoginRequired` (a ``FetchAuthError``) when no ChatGPT
    account is enrolled. There is nothing to refresh — Codex owns the tokens —
    so the scheduler treats it as a normal failure with an actionable message
    rather than attempting a token refresh.
    """
    _account, rate_limits = client.read_account_and_rate_limits()
    dump_json("codex_raw.json", rate_limits)
    return parse_rate_limits(rate_limits)
