from __future__ import annotations

from datetime import datetime, timezone

import httpx

from usage_dashboard.server.fetch_types import FetchError
from usage_dashboard.shared.models import (
    THROTTLE_BOXED,
    THROTTLE_LOW,
    THROTTLE_NONE,
    THROTTLE_RATE_LIMITED,
    Provider,
    Reading,
    ReadingStatus,
)

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
        usage: dict[str, object] = data.get("usage") or {}
        window: dict[str, object] = data.get("window") or {}

        requests_in_window = int(str(usage["requests_in_window"]))
        tokens_total = int(str(usage["tokens_in"])) + int(str(usage["tokens_out"]))

        resets_raw = window.get("resets_at")
        resets_at: datetime | None = (
            _to_naive_utc(datetime.fromisoformat(str(resets_raw)))
            if resets_raw is not None
            else None
        )

        # priority is umans' only severity signal (it has no usage quota).
        # boxed_until = penalty box: the account is locked for the window (worst,
        # so it wins). low = deprioritised routing, e.g. over the concurrency
        # threshold. Neither = normal.
        priority = usage.get("priority") or {}
        priority = priority if isinstance(priority, dict) else {}
        boxed_until_raw = priority.get("boxed_until")
        boxed_until = (
            _to_naive_utc(datetime.fromisoformat(str(boxed_until_raw)))
            if boxed_until_raw is not None
            else None
        )
        # Only an *unexpired* boxed_until means the account is actually boxed.
        # umans keeps returning the timestamp after the box lifts (notably after
        # a self-reactivation), so a boxed_until in the past must NOT latch us as
        # boxed — otherwise every reading reports "boxed" forever and the Pi
        # footer stays red even though the account is live again.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if boxed_until is not None and boxed_until > now:
            # priority.reason discriminates the rung: "rate_limited" means the
            # account keeps serving at low priority for the window (a limit
            # hit, not a lock — proven live 2026-07-03). Any other or absent
            # reason is treated as a hard box, fail safe.
            reason = priority.get("reason")
            if reason == "rate_limited":
                throttle = THROTTLE_RATE_LIMITED
            else:
                throttle = THROTTLE_BOXED
            # Either way the window-clear time is the actionable reset, so
            # surface boxed_until as the reading's reset; the client shows a
            # live countdown.
            resets_at = boxed_until
        elif priority.get("low"):
            throttle = THROTTLE_LOW
        else:
            throttle = THROTTLE_NONE
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
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
        throttle=throttle,
    )
