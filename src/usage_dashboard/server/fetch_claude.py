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
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus

_CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_TIMEOUT = 30.0

logger = logging.getLogger(__name__)


def _to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=None)


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
        five_hour = data["five_hour"]
        seven_day = data["seven_day"]
        session_percent: float | None = float(five_hour["utilization"])
        session_resets_at = _to_naive_utc(
            datetime.fromisoformat(five_hour["resets_at"]).replace(tzinfo=timezone.utc)
        )
        weekly_percent: float | None = float(seven_day["utilization"])
        weekly_resets_at = _to_naive_utc(
            datetime.fromisoformat(seven_day["resets_at"]).replace(tzinfo=timezone.utc)
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise FetchError(f"Claude usage response parse error: {type(exc).__name__}") from exc

    return Reading(
        provider=Provider.CLAUDE,
        status=ReadingStatus.CURRENT,
        session_percent=session_percent,
        session_resets_at=session_resets_at,
        weekly_percent=weekly_percent,
        weekly_resets_at=weekly_resets_at,
        fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
        stale=False,
    )
