from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from usage_dashboard.server.fetch_types import FetchError
from usage_dashboard.shared.models import (
    ALERT_CRIT,
    ALERT_NONE,
    ALERT_WARN,
    THROTTLE_BOXED,
    THROTTLE_LOW,
    THROTTLE_LOW_INTERACTIVITY,
    THROTTLE_NONE,
    THROTTLE_RATE_LIMITED,
    Provider,
    Reading,
    ReadingStatus,
)

logger = logging.getLogger(__name__)

_UMANS_USAGE_URL = "https://api.code.umans.ai/v1/usage"
_UMANS_HISTORY_URL = "https://api.code.umans.ai/v1/usage/history"
_TIMEOUT = 30.0

# The trailing window the detail line reports, and the token thresholds that
# colour it. umans' heavy-usage penalty (low-interactivity mode) keys off an
# undisclosed trailing-day volume, so the thresholds are empirical guesses —
# tune via UMANS_HISTORY_HOURS / UMANS_TOKENS_WARN / UMANS_TOKENS_CRIT rather
# than editing these. Defaults match sluice's dashboard tiers (warn 250M,
# crit 350M in+out).
DEFAULT_HISTORY_HOURS = 24
DEFAULT_TOKENS_WARN = 250_000_000
DEFAULT_TOKENS_CRIT = 350_000_000


@dataclass(frozen=True, slots=True)
class _HistorySums:
    requests: int
    tokens: int  # tokens_in + tokens_out


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


def _fetch_history_sums(
    client: httpx.Client, headers: dict[str, str], hours: int
) -> _HistorySums | None:
    """Sum requests + tokens over the trailing *hours* from /v1/usage/history.

    The endpoint returns hourly buckets ({requests, tokens_in, tokens_out});
    the sum is a ~window approximation (partial current hour, oldest bucket
    straddles the boundary). Telemetry, not truth path: any failure returns
    None and the caller falls back to the current-window counters.
    """
    now = datetime.now(timezone.utc)
    params = {
        "from": (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "granularity": "hour",
    }
    try:
        response = client.get(_UMANS_HISTORY_URL, headers=headers, params=params)
        response.raise_for_status()
        buckets = response.json().get("buckets")
        if not isinstance(buckets, list):
            raise ValueError("history response has no buckets list")
        requests = 0
        tokens = 0
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            requests += int(bucket.get("requests") or 0)
            tokens += int(bucket.get("tokens_in") or 0)
            tokens += int(bucket.get("tokens_out") or 0)
        return _HistorySums(requests=requests, tokens=tokens)
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.warning(
            "umans usage history fetch failed (%s); falling back to window counters",
            type(exc).__name__,
        )
        return None


def fetch_umans_usage(
    api_key: str,
    history_hours: int = DEFAULT_HISTORY_HOURS,
    tokens_warn: int = DEFAULT_TOKENS_WARN,
    tokens_crit: int = DEFAULT_TOKENS_CRIT,
) -> Reading:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.get(_UMANS_USAGE_URL, headers=headers)
            response.raise_for_status()
            data = response.json()
            # Trailing-window totals are the actionable signal for the opaque
            # heavy-day penalty; the per-window counters remain the fallback.
            sums = _fetch_history_sums(client, headers, history_hours)
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
        # service_mode is the heavy-day penalty (distinct from priority/boxed):
        # {current: "low_interactivity", resets_at: ISO} while requests queue
        # behind interactive sessions. Like boxed_until, only an unexpired
        # resets_at counts — don't latch on a stale timestamp.
        service_mode = usage.get("service_mode") or {}
        service_mode = service_mode if isinstance(service_mode, dict) else {}
        sm_resets_raw = service_mode.get("resets_at")
        sm_resets_at = (
            _to_naive_utc(datetime.fromisoformat(str(sm_resets_raw)))
            if sm_resets_raw is not None
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
        elif service_mode.get("current") == "low_interactivity" and (
            sm_resets_at is None or sm_resets_at > now
        ):
            throttle = THROTTLE_LOW_INTERACTIVITY
            # interactive-again is the actionable reset; the client counts down.
            if sm_resets_at is not None:
                resets_at = sm_resets_at
        elif priority.get("low"):
            throttle = THROTTLE_LOW
        else:
            throttle = THROTTLE_NONE
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise FetchError(f"umans usage response parse error: {type(exc).__name__}") from exc

    alert = ALERT_NONE
    if sums is not None:
        detail = f"{history_hours}h req {sums.requests}  tok {_format_tokens(sums.tokens)}"
        if sums.tokens >= tokens_crit:
            alert = ALERT_CRIT
        elif sums.tokens >= tokens_warn:
            alert = ALERT_WARN
    else:
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
        alert=alert,
    )
