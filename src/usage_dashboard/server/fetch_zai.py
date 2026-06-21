from __future__ import annotations

from datetime import datetime, timezone

import httpx

from usage_dashboard.server.fetch_types import FetchAuthError, FetchError, dump_json
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus

_ZAI_USAGE_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
_TIMEOUT = 30.0

# Observed live response (2026-06-12): the payload is wrapped in a
# {"code", "msg", "data"} envelope, nextResetTime is epoch milliseconds, and
# the relevant entries are TOKENS_LIMIT unit 3 (resets every 5h -> session)
# and TOKENS_LIMIT unit 6 (resets weekly). TIME_LIMIT unit 5 is the monthly
# API-tools quota, not coding usage.
_SESSION_UNIT = 3
_WEEKLY_UNIT = 6


def _from_epoch_ms(value: object) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(str(value)) / 1000, tz=timezone.utc).replace(tzinfo=None)


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
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 401:
            raise FetchAuthError("ZAI usage request rejected: HTTP 401") from exc
        raise FetchError(f"ZAI usage request failed: HTTP {status}") from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"ZAI usage request failed: {type(exc).__name__}") from exc

    dump_json("zai_raw.json", data)

    try:
        payload = data["data"] if isinstance(data.get("data"), dict) else data
        limits: list[dict[str, object]] = payload["limits"]
        session_entry: dict[str, object] | None = None
        weekly_entry: dict[str, object] | None = None
        for entry in limits:
            if entry.get("type") != "TOKENS_LIMIT":
                continue
            if entry.get("unit") == _SESSION_UNIT:
                session_entry = entry
            elif entry.get("unit") == _WEEKLY_UNIT:
                weekly_entry = entry
        if session_entry is None:
            raise FetchError("ZAI usage response missing session limit entry")
        if weekly_entry is None:
            raise FetchError("ZAI usage response missing weekly limit entry")
        session_percent: float | None = float(str(session_entry["percentage"]))
        session_resets_at = _from_epoch_ms(session_entry.get("nextResetTime"))
        weekly_percent: float | None = float(str(weekly_entry["percentage"]))
        weekly_resets_at = _from_epoch_ms(weekly_entry.get("nextResetTime"))
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
