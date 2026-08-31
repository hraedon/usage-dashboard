"""Fetch OpenCode Go (opencode.ai) workspace usage.

Same shape as the Ollama fetcher: there is no usage API, so the workspace
dashboard page is scraped with a browser session cookie pasted into a secret.
Two credentials are needed — the workspace id (``wrk_…``, stable) and the
``auth`` cookie (opaque, rotates on re-login).

Parsing mirrors slkiser/opencode-quota's ``src/lib/opencode-go.ts``, the
maintained prior art, and was re-verified against the live page 2026-08-07.
The page is SolidJS SSR and carries the numbers **twice**:

1. A hydration blob with exact values, which is what we prefer::

       rollingUsage:$R[36]={status:"ok",resetInSec:17223,usagePercent:0},
       weeklyUsage:$R[37]={status:"ok",resetInSec:256748,usagePercent:13},
       monthlyUsage:$R[38]={status:"ok",resetInSec:2306645,usagePercent:7}

2. The rendered markup, whose countdown is rounded to whole units, used as a
   fallback if the hydration format ever changes::

       <span data-slot="usage-label">Rolling Usage</span>
       <span data-slot="usage-value"><!--$-->0<!--/-->%</span>
       <span data-slot="reset-time"><!--$-->Resets in<!--/-->
         <!--$-->4 hours 47 minutes<!--/--></span>

The prior art matches the hydration blob with two regexes per window (one per
field ordering). We instead capture the whole ``{…}`` body once and pull named
fields out of it, which is order-independent and survives new fields being
added alongside ``status``.

**The three windows map to:** rolling (~5 h) → ``session``, weekly → ``weekly``,
monthly → a :class:`ScopedLimit` named "Monthly" (an extra bar). Only rolling
and weekly have first-class fields on :class:`Reading`.

**Auth failures are ambiguous, by design of the site.** An expired cookie *and*
a wrong workspace id both redirect to ``auth.opencode.ai/authorize`` and return
HTTP 200 on the final hop — there is no 401 and no distinguishing marker
(verified live). So the auth error names both causes rather than asserting a
re-login is needed.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from usage_dashboard.server.fetch_types import FetchAuthError, FetchError, debug_dump
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus, ScopedLimit

_DASHBOARD_URL = "https://opencode.ai/workspace/{workspace_id}/go"
_AUTH_HOST_SUFFIX = "auth.opencode.ai"
_TIMEOUT = 30.0
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# Window keys as they appear in both the hydration blob and (lowercased) the
# rendered usage labels, in display order.
_ROLLING = "rolling"
_WEEKLY = "weekly"
_MONTHLY = "monthly"
_WINDOW_KEYS = (_ROLLING, _WEEKLY, _MONTHLY)

_NUMBER = r"(-?\d+(?:\.\d+)?)"

# `<key>Usage:$R[N]={...}` — the braces hold no nested object, so [^{}]* is a
# safe body match. Anchoring on `$R[` is what keeps this from matching the
# unrelated `monthlyUsage:null` that appears in the billing object earlier on
# the page.
_SSR_WINDOW_RES = {
    key: re.compile(rf"{key}Usage:\$R\[\d+\]=\{{([^{{}}]*)\}}")
    for key in _WINDOW_KEYS
}
_SSR_PERCENT_RE = re.compile(rf"usagePercent:{_NUMBER}")
_SSR_RESET_RE = re.compile(rf"resetInSec:{_NUMBER}")

# Rendered-markup fallback. Each window is a `data-slot="usage-item"` block
# holding a label, a percentage, and either a reset countdown or a "reset-now".
_ITEM_SPLIT_RE = re.compile(r'data-slot="usage-item"')
_ITEM_LABEL_RE = re.compile(r'data-slot="usage-label">([^<]+)<')
_ITEM_PERCENT_RE = re.compile(r'data-slot="usage-value">[^0-9]*(\d+(?:\.\d+)?)')
_ITEM_RESET_RE = re.compile(
    r'data-slot="(reset-time|reset-now)">(.*?)</span>', re.DOTALL
)
# SolidJS hydration markers interleaved with the text nodes.
_HYDRATION_COMMENT_RE = re.compile(r"<!--\$-->|<!--/-->")
_RESETS_IN_PREFIX_RE = re.compile(r"resets?\s+in\s*", re.IGNORECASE)
_DURATION_TOKEN_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(day|hour|hr|minute|min|second|sec)s?\b", re.IGNORECASE
)
_DURATION_UNITS = {
    "day": 86400.0,
    "hour": 3600.0,
    "hr": 3600.0,
    "minute": 60.0,
    "min": 60.0,
    "second": 1.0,
    "sec": 1.0,
}
_DURATION_GRANULARITY = {
    "day": 86400,
    "hour": 3600,
    "hr": 3600,
    "minute": 60,
    "min": 60,
    "second": 1,
    "sec": 1,
}


class _Window:
    """One parsed usage window: a percentage and seconds until it resets."""

    __slots__ = ("percent", "reset_in_seconds", "reset_granularity")

    def __init__(
        self,
        percent: float,
        reset_in_seconds: float | None,
        reset_granularity: int | None = None,
    ) -> None:
        # The site has no reason to report a negative percentage or countdown,
        # but clamping keeps a glitched value from rendering as a bar running
        # backwards or a reset time in the past.
        self.percent = max(0.0, percent)
        self.reset_in_seconds = (
            None if reset_in_seconds is None else max(0.0, reset_in_seconds)
        )
        self.reset_granularity = reset_granularity

    def resets_at(self, now: datetime) -> datetime | None:
        if self.reset_in_seconds is None:
            return None
        return now + timedelta(seconds=self.reset_in_seconds)


def _parse_ssr_windows(html: str) -> dict[str, _Window]:
    """Parse the SolidJS hydration blob (exact values)."""
    windows: dict[str, _Window] = {}
    for key, pattern in _SSR_WINDOW_RES.items():
        match = pattern.search(html)
        if match is None:
            continue
        body = match.group(1)
        percent_m = _SSR_PERCENT_RE.search(body)
        if percent_m is None:
            continue
        reset_m = _SSR_RESET_RE.search(body)
        windows[key] = _Window(
            percent=float(percent_m.group(1)),
            reset_in_seconds=float(reset_m.group(1)) if reset_m else None,
        )
    return windows


def _parse_duration_seconds(text: str) -> float | None:
    """Sum a human countdown ("6 days 2 hours") into seconds.

    Returns None when no duration token is present, so an unparseable string is
    distinguishable from a genuine zero.
    """
    tokens = _DURATION_TOKEN_RE.findall(text)
    if not tokens:
        return None
    return sum(float(amount) * _DURATION_UNITS[unit.lower()] for amount, unit in tokens)


def _parse_duration_granularity(text: str) -> int | None:
    """Return the smallest unit carried by a rendered countdown.

    The hydration values are exact ``resetInSec`` numbers. The markup fallback
    is rounded to the units it displays (for example, ``4 hours 47 minutes``
    has minute resolution), so the scheduler needs that resolution to avoid
    treating the recomputed absolute timestamp as usage movement on every
    poll. ``None`` means the text had no duration token.
    """
    tokens = _DURATION_TOKEN_RE.findall(text)
    if not tokens:
        return None
    return min(_DURATION_GRANULARITY[unit.lower()] for _amount, unit in tokens)


def _window_key_for_label(label: str) -> str | None:
    lowered = label.strip().lower()
    for key in _WINDOW_KEYS:
        if key in lowered:
            return key
    return None


def _parse_markup_windows(html: str) -> dict[str, _Window]:
    """Parse the rendered ``data-slot`` markup (countdowns rounded to units)."""
    windows: dict[str, _Window] = {}
    for chunk in _ITEM_SPLIT_RE.split(html)[1:]:
        label_m = _ITEM_LABEL_RE.search(chunk)
        percent_m = _ITEM_PERCENT_RE.search(chunk)
        if label_m is None or percent_m is None:
            continue
        key = _window_key_for_label(label_m.group(1))
        if key is None or key in windows:
            continue
        reset_seconds: float | None = None
        reset_granularity: int | None = None
        reset_m = _ITEM_RESET_RE.search(chunk)
        if reset_m is not None:
            if reset_m.group(1) == "reset-now":
                reset_seconds = 0.0
                reset_granularity = None
            else:
                text = _HYDRATION_COMMENT_RE.sub("", reset_m.group(2))
                countdown = _RESETS_IN_PREFIX_RE.sub("", text).strip()
                reset_seconds = _parse_duration_seconds(countdown)
                reset_granularity = _parse_duration_granularity(countdown)
        windows[key] = _Window(
            percent=float(percent_m.group(1)),
            reset_in_seconds=reset_seconds,
            reset_granularity=reset_granularity,
        )
    return windows


def fetch_opencode_usage(workspace_id: str, cookie: str) -> Reading:
    """Scrape the OpenCode Go workspace page into a Reading.

    *cookie* is the bare ``auth`` cookie value (no ``auth=`` prefix); it is sent
    as ``Cookie: auth=<value>``.
    """
    if not workspace_id:
        raise FetchError("OpenCode Go workspace id is not configured")
    url = _DASHBOARD_URL.format(workspace_id=quote(workspace_id, safe=""))
    headers = {
        "Cookie": f"auth={cookie}",
        "User-Agent": _USER_AGENT,
        "Accept": "text/html",
    }
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = client.get(url, headers=headers)
        if response.status_code in (401, 403):
            raise FetchAuthError(
                f"OpenCode Go session rejected: HTTP {response.status_code}"
            )
        # Signed out / unknown workspace: the site 302s to its auth host and
        # serves the sign-in page with a 200, so the redirect target is the
        # only reliable signal.
        host = response.url.host
        if host == _AUTH_HOST_SUFFIX or host.endswith("." + _AUTH_HOST_SUFFIX):
            raise FetchAuthError(
                "OpenCode Go redirected to sign-in — the auth cookie has "
                "expired or the workspace id is wrong (the site does not "
                "distinguish the two)"
            )
        response.raise_for_status()
        html = response.text
    except httpx.HTTPStatusError as exc:
        raise FetchError(
            f"OpenCode Go request failed: HTTP {exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"OpenCode Go request failed: {type(exc).__name__}") from exc

    debug_dump("opencode_raw.html", html)

    windows = _parse_ssr_windows(html)
    if not windows:
        windows = _parse_markup_windows(html)
    if not windows:
        raise FetchError(
            "OpenCode Go page missing usage data (neither the hydration blob "
            "nor the usage-item markup parsed)"
        )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rolling = windows.get(_ROLLING)
    weekly = windows.get(_WEEKLY)
    monthly = windows.get(_MONTHLY)

    # Monthly is a third aggregate window with no first-class field on Reading;
    # scoped_limits is the model's "extra bars beyond session/weekly" slot.
    scoped_limits = (
        [
            ScopedLimit(
                name="Monthly",
                percent=monthly.percent,
                resets_at=monthly.resets_at(now),
                reset_granularity=monthly.reset_granularity,
            )
        ]
        if monthly is not None
        else None
    )

    return Reading(
        provider=Provider.OPENCODE,
        status=ReadingStatus.CURRENT,
        session_percent=rolling.percent if rolling else None,
        session_resets_at=rolling.resets_at(now) if rolling else None,
        weekly_percent=weekly.percent if weekly else None,
        weekly_resets_at=weekly.resets_at(now) if weekly else None,
        fetched_at=now,
        stale=False,
        scoped_limits=scoped_limits,
        session_reset_granularity=rolling.reset_granularity if rolling else None,
        weekly_reset_granularity=weekly.reset_granularity if weekly else None,
    )
