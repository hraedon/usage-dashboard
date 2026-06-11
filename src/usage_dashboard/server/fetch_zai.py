from __future__ import annotations

from datetime import datetime, timezone

import httpx

from usage_dashboard.server.fetch_types import FetchError
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus

_ZAI_USAGE_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
_TIMEOUT = 30.0


def _to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=None)


def fetch_zai_usage(api_key: str) -> Reading:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.get(_ZAI_USAGE_URL, headers=headers)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        raise FetchError(f"ZAI usage request failed: {type(exc).__name__}") from exc

    try:
        limits: list[dict[str, object]] = data["limits"]
        session_entry: dict[str, object] | None = None
        weekly_entry: dict[str, object] | None = None
        for entry in limits:
            entry_type = entry.get("type")
            unit = entry.get("unit")
            if entry_type == "TIME_LIMIT" and unit == 5:
                session_entry = entry
            elif entry_type == "TOKENS_LIMIT" and unit == 6:
                weekly_entry = entry
        if session_entry is None:
            raise FetchError("ZAI usage response missing session limit entry")
        if weekly_entry is None:
            raise FetchError("ZAI usage response missing weekly limit entry")
        session_percent: float | None = float(str(session_entry["percentage"]))
        session_resets_raw = session_entry.get("nextResetTime")
        session_resets_at: datetime | None = (
            _to_naive_utc(datetime.fromisoformat(str(session_resets_raw)).replace(tzinfo=timezone.utc))
            if session_resets_raw is not None
            else None
        )
        weekly_percent: float | None = float(str(weekly_entry["percentage"]))
        weekly_resets_raw = weekly_entry.get("nextResetTime")
        weekly_resets_at: datetime | None = (
            _to_naive_utc(datetime.fromisoformat(str(weekly_resets_raw)).replace(tzinfo=timezone.utc))
            if weekly_resets_raw is not None
            else None
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise FetchError(f"ZAI usage response parse error: {type(exc).__name__}") from exc

    return Reading(
        provider=Provider.ZAI,
        status=ReadingStatus.CURRENT,
        session_percent=session_percent,
        session_resets_at=session_resets_at,
        weekly_percent=weekly_percent,
        weekly_resets_at=weekly_resets_at,
        fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
        stale=False,
    )
