from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from usage_dashboard.server.fetch_types import (
    FetchAuthError,
    FetchError,
    FetchRateLimitError,
    dump_json,
)
from usage_dashboard.shared.models import (
    Provider,
    Reading,
    ReadingStatus,
    ScopedLimit,
)

_CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_TIMEOUT = 30.0

logger = logging.getLogger(__name__)


def _to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=None)


def _extract_window(block: object) -> tuple[float | None, datetime | None]:
    """Pull (utilization%, resets_at) from a usage window block.

    The /oauth/usage endpoint returns ``utilization`` and ``resets_at`` as
    ``null`` when the window is idle (e.g. no Claude activity in the trailing
    five hours), and may omit the block entirely. The old code assumed both
    fields were always present and non-null, so ``float(None)`` /
    ``fromisoformat(None)`` raised TypeError and the provider went stale until
    activity resumed. Treat an absent/null value as "unknown" (None), which the
    model and renderers already handle (gray bar / "N/A").
    """
    if not isinstance(block, dict):
        return None, None
    util = block.get("utilization")
    percent = float(util) if util is not None else None
    resets_raw = block.get("resets_at")
    resets_at = (
        _to_naive_utc(datetime.fromisoformat(resets_raw).replace(tzinfo=timezone.utc))
        if resets_raw is not None
        else None
    )
    return percent, resets_at


def _extract_scoped_limits(limits: object) -> list[ScopedLimit]:
    """Pull per-model usage windows from the ``limits[]`` array.

    Only entries whose ``scope.model.display_name`` is set are surfaced — these
    are the ``weekly_scoped`` per-model limits (e.g. Fable). The unscoped
    ``session``/``weekly_all`` entries duplicate the top-level ``five_hour`` /
    ``seven_day`` blocks the aggregate bars already use, so they're skipped. A
    malformed or absent array yields an empty list rather than raising — scoped
    limits are additive, and their loss shouldn't fail an otherwise-good fetch.
    """
    if not isinstance(limits, list):
        return []
    scoped: list[ScopedLimit] = []
    for item in limits:
        if not isinstance(item, dict):
            continue
        scope = item.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        name = model.get("display_name") if isinstance(model, dict) else None
        if not name:
            continue
        pct = item.get("percent")
        percent = float(pct) if pct is not None else None
        resets_raw = item.get("resets_at")
        resets_at = (
            _to_naive_utc(datetime.fromisoformat(resets_raw))
            if isinstance(resets_raw, str)
            else None
        )
        scoped.append(
            ScopedLimit(
                name=name,
                percent=percent,
                resets_at=resets_at,
                is_active=bool(item.get("is_active")),
            )
        )
    return scoped


def refresh_claude_token(
    refresh_token: str,
    client_id: str | None = None,
) -> tuple[str, str]:
    payload: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    if client_id is not None:
        payload["client_id"] = client_id
    try:
        response = httpx.post(
            _CLAUDE_TOKEN_URL,
            json=payload,
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        new_access = data["access_token"]
        new_refresh = data.get("refresh_token", refresh_token)
        return new_access, new_refresh
    except httpx.HTTPError as exc:
        raise FetchError(
            f"Claude token refresh failed: {type(exc).__name__}"
        ) from exc
    except (KeyError, ValueError, TypeError) as exc:
        raise FetchError(
            f"Claude token refresh parse error: {type(exc).__name__}"
        ) from exc


def fetch_claude_usage(access_token: str) -> Reading:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": "oauth-2025-04-20",
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.get(_CLAUDE_USAGE_URL, headers=headers)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 401:
            # Expired/invalid token — refreshable.
            raise FetchAuthError("Claude usage request rejected: HTTP 401") from exc
        if status == 403:
            # Authenticated but wrong scope/permission (e.g. a setup-token
            # lacks user:profile). Permanent — refreshing cannot fix it, so
            # this is a plain FetchError and must NOT trigger a token refresh.
            raise FetchError(
                "Claude usage request forbidden: HTTP 403 "
                "(token lacks the user:profile scope the usage endpoint needs)"
            ) from exc
        if status == 429:
            retry_after: float | None = None
            header = exc.response.headers.get("retry-after")
            if header is not None and header.isdigit():
                retry_after = float(header)
            raise FetchRateLimitError(
                f"Claude usage request rate limited: HTTP 429 (retry-after {header})",
                retry_after_seconds=retry_after,
            ) from exc
        raise FetchError(f"Claude usage request failed: HTTP {status}") from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"Claude usage request failed: {type(exc).__name__}") from exc

    dump_json("claude_raw.json", data)

    try:
        # Subscript (not .get) so a response missing these keys entirely is
        # still treated as malformed (KeyError -> FetchError); a present-but-null
        # block is the idle case _extract_window tolerates.
        session_percent, session_resets_at = _extract_window(data["five_hour"])
        weekly_percent, weekly_resets_at = _extract_window(data["seven_day"])
    except (KeyError, ValueError, TypeError) as exc:
        raise FetchError(f"Claude usage response parse error: {type(exc).__name__}") from exc

    # Per-model scoped windows (e.g. Fable) live in limits[]; absence is normal.
    scoped_limits = _extract_scoped_limits(data.get("limits"))

    return Reading(
        provider=Provider.CLAUDE,
        status=ReadingStatus.CURRENT,
        session_percent=session_percent,
        session_resets_at=session_resets_at,
        weekly_percent=weekly_percent,
        weekly_resets_at=weekly_resets_at,
        fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
        stale=False,
        scoped_limits=scoped_limits or None,
    )
