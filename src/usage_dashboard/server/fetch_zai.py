from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from usage_dashboard.server.fetch_types import FetchAuthError, FetchError, dump_json
from usage_dashboard.shared.models import (
    ALERT_CRIT,
    ALERT_NONE,
    ALERT_WARN,
    ModelUsage,
    Provider,
    Reading,
    ReadingStatus,
)

logger = logging.getLogger(__name__)

_ZAI_USAGE_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
# Token totals per day over an arbitrary window (the current weekly quota
# window). Discovered from the subscription page's own bundle 2026-08-03:
# params are startTime/endTime in "YYYY-MM-DD HH:MM:SS" plus granularity.
_ZAI_MODEL_USAGE_URL = "https://api.z.ai/api/monitor/usage/model-usage"
# The model-usage endpoint speaks UTC+8 wall clock (probed live 2026-08-03).
_ZAI_API_UTC_OFFSET = timedelta(hours=8)
# Length of the weekly quota window, used to derive its start from the reported
# next reset time.
_WEEKLY_WINDOW = timedelta(days=7)
_TIMEOUT = 30.0

# Observed live response (2026-06-12): the payload is wrapped in a
# {"code", "msg", "data"} envelope, nextResetTime is epoch milliseconds, and
# the relevant entries are TOKENS_LIMIT unit 3 (resets every 5h -> session)
# and TOKENS_LIMIT unit 6 (resets weekly). TIME_LIMIT unit 5 is the monthly
# API-tools quota, not coding usage.
_SESSION_UNIT = 3
_WEEKLY_UNIT = 6
_TOOLS_UNIT = 5  # TIME_LIMIT: monthly API-tools quota (search-prime, web-reader, etc.)

# Weekly-window token thresholds that colour the z.ai detail line
# (warn/orange, crit/red). The weekly cap is roughly API-value equivalent to
# 15–30× the monthly fee (Pro ≈ 300M/week as of 2026-08), so the defaults are
# ~80% / ~95% of a Pro week. Empirical — tune via ZAI_WEEK_TOKENS_WARN /
# ZAI_WEEK_TOKENS_CRIT rather than editing these.
DEFAULT_TOKENS_WARN = 240_000_000
DEFAULT_TOKENS_CRIT = 285_000_000


@dataclass(frozen=True, slots=True)
class _WindowSums:
    """Requests + tokens summed over the current weekly quota window."""

    requests: int
    tokens: int


def _format_tokens(count: int) -> str:
    if count >= 1_000_000_000:
        return f"{count / 1_000_000_000:.1f}B"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def _to_api_clock(moment: datetime) -> str:
    """Format a naive-UTC *moment* on the model-usage endpoint's own clock."""
    return (moment + _ZAI_API_UTC_OFFSET).strftime("%Y-%m-%d %H:%M:%S")


def _to_int(value: object, default: int = 0) -> int:
    """int() that tolerates the API's numeric strings and whole-valued floats.

    ``int(str(x))`` raises on a float (``int("123.0")`` is a ValueError), so a
    payload that ever serialises a counter as ``123.0`` would fail the whole
    fetch; JSON numbers are otherwise handled natively.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else default
    try:
        return int(float(str(value).strip()))
    except ValueError:
        return default


def _from_epoch_ms(value: object) -> datetime | None:
    """Convert a z.ai epoch-millisecond value to naive UTC.

    Missing reset fields are valid for an idle window. A present but malformed
    value is not: silently defaulting it to zero manufactures a 1970 reset and
    makes the scheduler/client treat bad provider data as real data. Preserve
    the fetcher's existing malformed-response path by raising ``ValueError``
    for nonempty invalid values; ``fetch_zai_usage`` turns that into a
    ``FetchError``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("epoch milliseconds must be numeric")
    if isinstance(value, int):
        epoch_ms = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError("epoch milliseconds must be a finite whole number")
        epoch_ms = int(value)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise ValueError("epoch milliseconds must not be empty")
        try:
            parsed = float(raw)
        except ValueError as exc:
            raise ValueError("epoch milliseconds must be numeric") from exc
        if not math.isfinite(parsed) or not parsed.is_integer():
            raise ValueError("epoch milliseconds must be a finite whole number")
        epoch_ms = int(parsed)
    else:
        raise ValueError("epoch milliseconds must be numeric")
    try:
        return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).replace(
            tzinfo=None
        )
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("epoch milliseconds are outside the datetime range") from exc


def _fetch_weekly_sums(
    client: httpx.Client,
    headers: dict[str, str],
    start: datetime,
    end: datetime,
) -> _WindowSums | None:
    """Requests + tokens over [start, end] via /api/monitor/usage/model-usage.

    *start* and *end* are naive UTC; they are converted to UTC+8 for the wire.
    Live probe 2026-08-03: the endpoint's clock is UTC+8 — an ``endTime`` of
    05:44 UTC came back with a last bucket labelled 13:00 — and it clamps
    ``endTime`` to its own "now" (sending now+9h changed nothing). Sending
    naive UTC therefore shifts the window boundary 8h earlier than intended.

    Totals come from ``data.totalUsage`` rather than summing the per-bucket
    arrays: the same probe showed ``granularity=day`` is **ignored** (the
    response came back ``hourly``), so bucket semantics are not something to
    depend on. The arrays are summed only as a fallback; both agreed exactly
    when checked. Follows the retired umans history pattern (a second GET
    whose failure degrades the reading rather than failing it): any error
    returns None and the token line is omitted — the percentage bars remain
    the truth path.
    """
    if start >= end:
        logger.warning(
            "z.ai model-usage window is empty or inverted (%s -> %s); skipping",
            start,
            end,
        )
        return None
    params = {
        "startTime": _to_api_clock(start),
        "endTime": _to_api_clock(end),
        "granularity": "day",
    }
    try:
        response = client.get(_ZAI_MODEL_USAGE_URL, headers=headers, params=params)
        response.raise_for_status()
        payload = response.json()
        data = payload["data"] if isinstance(payload.get("data"), dict) else payload
        totals = data.get("totalUsage")
        if isinstance(totals, dict) and "totalTokensUsage" in totals:
            return _WindowSums(
                requests=int(totals.get("totalModelCallCount") or 0),
                tokens=int(totals["totalTokensUsage"] or 0),
            )
        tokens_raw = data["tokensUsage"]
        calls_raw = data["modelCallCount"]
        if not isinstance(tokens_raw, list) or not isinstance(calls_raw, list):
            raise ValueError("model-usage response missing tokensUsage/modelCallCount")
        tokens = sum(int(v or 0) for v in tokens_raw)
        requests = sum(int(v or 0) for v in calls_raw)
        return _WindowSums(requests=requests, tokens=tokens)
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        logger.warning(
            "z.ai model-usage fetch failed (%s); omitting weekly token line",
            type(exc).__name__,
        )
        return None


def fetch_zai_usage(
    api_key: str,
    tokens_warn: int = DEFAULT_TOKENS_WARN,
    tokens_crit: int = DEFAULT_TOKENS_CRIT,
) -> Reading:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    sums: _WindowSums | None = None
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.get(_ZAI_USAGE_URL, headers=headers)
            response.raise_for_status()
            data = response.json()
            dump_json("zai_raw.json", data)

            payload = data["data"] if isinstance(data.get("data"), dict) else data
            limits: list[dict[str, object]] = payload["limits"]
            session_entry: dict[str, object] | None = None
            weekly_entry: dict[str, object] | None = None
            tools_entry: dict[str, object] | None = None
            for entry in limits:
                if entry.get("type") == "TOKENS_LIMIT":
                    if entry.get("unit") == _SESSION_UNIT:
                        session_entry = entry
                    elif entry.get("unit") == _WEEKLY_UNIT:
                        weekly_entry = entry
                elif entry.get("type") == "TIME_LIMIT" and entry.get("unit") == _TOOLS_UNIT:
                    tools_entry = entry
            if session_entry is None:
                raise FetchError("ZAI usage response missing session limit entry")
            if weekly_entry is None:
                raise FetchError("ZAI usage response missing weekly limit entry")
            session_percent: float | None = float(str(session_entry["percentage"]))
            session_resets_at = _from_epoch_ms(session_entry.get("nextResetTime"))
            weekly_percent: float | None = float(str(weekly_entry["percentage"]))
            weekly_resets_at = _from_epoch_ms(weekly_entry.get("nextResetTime"))

            # Parse the monthly API-tools quota's per-model breakdown, if present.
            models: list[ModelUsage] | None = None
            if tools_entry is not None:
                details_raw = tools_entry.get("usageDetails")
                details: list[dict[str, object]] = (
                    details_raw if isinstance(details_raw, list) else []
                )
                total_used = _to_int(tools_entry.get("currentValue"))
                model_list = [
                    ModelUsage(
                        name=str(d.get("modelCode", "")),
                        requests=_to_int(d.get("usage")),
                        share_percent=(
                            _to_int(d.get("usage")) / total_used * 100
                            if total_used > 0
                            else 0.0
                        ),
                    )
                    for d in details
                    if d.get("modelCode")
                ]
                model_list.sort(key=lambda m: m.share_percent, reverse=True)
                models = model_list if model_list else None

            # Weekly-window token totals (model-usage), following the retired
            # umans pattern: a second GET sums the window and the reading's
            # detail + alert carry the result. Window = the current weekly
            # quota window (weekly_resets_at -> now); telemetry, so a failure
            # leaves detail None rather than failing the reading.
            if weekly_resets_at is not None:
                # nextResetTime is when the weekly quota NEXT resets — a future
                # instant — so the window that is currently accruing STARTED one
                # window-length before it. Passing the reset time itself as the
                # start inverts the range: probed live, that returns HTTP 200
                # with tokensUsage=None, which the guard below turns into "no
                # line at all", so the feature silently never worked.
                sums = _fetch_weekly_sums(
                    client,
                    headers,
                    weekly_resets_at - _WEEKLY_WINDOW,
                    datetime.now(timezone.utc).replace(tzinfo=None),
                )
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 401:
            raise FetchAuthError("ZAI usage request rejected: HTTP 401") from exc
        raise FetchError(f"ZAI usage request failed: HTTP {status}") from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"ZAI usage request failed: {type(exc).__name__}") from exc
    except (KeyError, ValueError, TypeError) as exc:
        raise FetchError(f"ZAI usage response parse error: {type(exc).__name__}") from exc

    detail = None
    alert = ALERT_NONE
    if sums is not None:
        detail = f"week req {sums.requests}  tok {_format_tokens(sums.tokens)}"
        if sums.tokens >= tokens_crit:
            alert = ALERT_CRIT
        elif sums.tokens >= tokens_warn:
            alert = ALERT_WARN

    return Reading(
        provider=Provider.ZAI,
        status=ReadingStatus.CURRENT,
        session_percent=session_percent,
        session_resets_at=session_resets_at,
        weekly_percent=weekly_percent,
        weekly_resets_at=weekly_resets_at,
        fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
        stale=False,
        models=models,
        detail=detail,
        alert=alert,
    )
