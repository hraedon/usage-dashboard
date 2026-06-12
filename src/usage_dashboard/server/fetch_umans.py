from __future__ import annotations

from datetime import datetime, timezone

import httpx

from usage_dashboard.server.fetch_types import FetchError
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus

_UMANS_USAGE_URL = "https://api.code.umans.ai/v1/usage"
_TIMEOUT = 30.0


def _format_tokens(count: int) -> str:
    if count >= 1_000_000_000:
        return f"{count / 1_000_000_000:.1f}B"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def _to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=None)


def fetch_umans_usage(api_key: str) -> Reading:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.get(_UMANS_USAGE_URL, headers=headers)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        raise FetchError(f"umans usage request failed: {type(exc).__name__}") from exc

    try:
        usage: dict[str, object] = data["usage"]
        window: dict[str, object] = data["window"]

        requests_in_window = int(str(usage["requests_in_window"]))
        tokens_total = int(str(usage["tokens_in"])) + int(str(usage["tokens_out"]))

        resets_raw = window.get("resets_at")
        resets_at: datetime | None = (
            _to_naive_utc(datetime.fromisoformat(str(resets_raw)))
            if resets_raw is not None
            else None
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise FetchError(f"umans usage response parse error: {type(exc).__name__}") from exc

    detail = f"req {requests_in_window}  tok {_format_tokens(tokens_total)}"

    return Reading(
        provider=Provider.UMANS,
        status=ReadingStatus.CURRENT,
        session_percent=None,
        session_resets_at=resets_at,
        weekly_percent=None,
        weekly_resets_at=None,
        fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
        stale=False,
        detail=detail,
    )
