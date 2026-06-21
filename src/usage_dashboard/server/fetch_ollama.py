from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import httpx

from usage_dashboard.server.fetch_types import FetchAuthError, FetchError, debug_dump
from usage_dashboard.shared.models import ModelUsage, Provider, Reading, ReadingStatus

_OLLAMA_SETTINGS_URL = "https://ollama.com/settings"
_TIMEOUT = 30.0
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# ollama.com has no usage API; the settings page is scraped with a browser
# session cookie (pasted into the ollama-cookie secret). Parsing mirrors
# steipete/CodexBar's OllamaUsageParser, the maintained prior art.
_SESSION_LABELS = ("Session usage", "Hourly usage")
_WEEKLY_LABEL = "Weekly usage"
# The reset countdown can sit far after its label — the weekly bar renders a row
# of segment <button>s between "Weekly usage" and its "Resets in …" text (~4.6k
# chars observed), and it's the last section so there's no following label to
# bound the block. Keep this comfortably above that gap or the weekly reset is
# silently dropped. (Earlier-section blocks are bounded by the next label, not
# this cap, so a generous value is safe.)
_BLOCK_MAX_CHARS = 8000

_PERCENT_USED_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%\s*used", re.IGNORECASE)
_BAR_WIDTH_RE = re.compile(r"width:\s*([0-9]+(?:\.[0-9]+)?)%", re.IGNORECASE)
_DATA_TIME_RE = re.compile(r'data-time="([^"]+)"')

# ollama.com renders reset times as a relative countdown ("Resets in 5 hours",
# "Resets in 2 days"), not an absolute timestamp, so parse that into an absolute
# time relative to the scrape. Captures the short span after "resets in" and
# sums any day/hour/minute tokens in it (handles "1 day 3 hours").
_RESET_IN_RE = re.compile(r"resets?\s+in\s+(.{0,40}?)(?:<|\bago\b|$)", re.IGNORECASE | re.DOTALL)
_DURATION_TOKEN_RE = re.compile(
    r"(\d+)\s*(week|day|hour|hr|minute|min)s?\b", re.IGNORECASE
)

# Per-model segment buttons in the weekly usage bar. Each <button> carries
# data-model, data-requests, and a style="width: N%" — the model's share of
# the bar. We match the full opening tag, then extract the three fields.
_SEGMENT_TAG_RE = re.compile(r"<button\b[^>]*data-usage-segment[^>]*>", re.DOTALL)
_DURATION_UNITS = {
    "week": "weeks",
    "day": "days",
    "hour": "hours",
    "hr": "hours",
    "minute": "minutes",
    "min": "minutes",
}

_SIGNED_OUT_MARKERS = (
    "sign in to ollama",
    "log in to ollama",
    "/api/auth/signin",
    'href="/signin"',
    'action="/signin"',
)


def _looks_signed_out(html: str) -> bool:
    lower = html.lower()
    return any(marker in lower for marker in _SIGNED_OUT_MARKERS)


def _block_after(label: str, html: str) -> str | None:
    start = html.find(label)
    if start == -1:
        return None
    tail = html[start + len(label) :]
    boundaries = [
        tail.find(other)
        for other in (*_SESSION_LABELS, _WEEKLY_LABEL)
        if other != label and tail.find(other) != -1
    ]
    end = min(boundaries) if boundaries else _BLOCK_MAX_CHARS
    return tail[: min(end, _BLOCK_MAX_CHARS)]


def _parse_percent(block: str) -> float | None:
    match = _PERCENT_USED_RE.search(block) or _BAR_WIDTH_RE.search(block)
    if match:
        return float(match.group(1))
    return None


def _parse_relative_reset(block: str, now: datetime) -> datetime | None:
    phrase = _RESET_IN_RE.search(block)
    if phrase is None:
        return None
    tokens = _DURATION_TOKEN_RE.findall(phrase.group(1))
    if not tokens:
        return None
    delta: dict[str, int] = {}
    for amount, unit in tokens:
        key = _DURATION_UNITS[unit.lower()]
        delta[key] = delta.get(key, 0) + int(amount)
    return now + timedelta(**delta)


def _parse_resets_at(block: str, now: datetime) -> datetime | None:
    # Prefer an explicit absolute timestamp if the markup ever carries one;
    # otherwise fall back to the relative "Resets in ..." countdown text.
    match = _DATA_TIME_RE.search(block)
    if match is not None:
        raw = match.group(1)
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc)
            return parsed.replace(tzinfo=None)
    return _parse_relative_reset(block, now)


def _parse_usage_block(
    labels: tuple[str, ...], html: str, now: datetime
) -> tuple[float, datetime | None] | None:
    for label in labels:
        block = _block_after(label, html)
        if block is None:
            continue
        percent = _parse_percent(block)
        if percent is not None:
            return percent, _parse_resets_at(block, now)
    return None


def _parse_model_segments(html: str) -> list[ModelUsage]:
    """Extract per-model usage from the weekly bar's segment buttons.

    Each ``<button data-usage-segment>`` carries the model name, request count,
    and bar-share width. Returns models sorted by share descending.
    """
    models: list[ModelUsage] = []
    for tag in _SEGMENT_TAG_RE.findall(html):
        model_m = re.search(r'data-model="([^"]+)"', tag)
        req_m = re.search(r'data-requests="(\d+)"', tag)
        width_m = re.search(r"width:\s*([0-9.]+)%", tag)
        if model_m and req_m and width_m:
            models.append(
                ModelUsage(
                    name=model_m.group(1),
                    requests=int(req_m.group(1)),
                    share_percent=float(width_m.group(1)),
                )
            )
    models.sort(key=lambda m: m.share_percent, reverse=True)
    return models


def fetch_ollama_usage(cookie: str) -> Reading:
    headers = {
        "Cookie": cookie,
        "User-Agent": _USER_AGENT,
        "Accept": "text/html",
    }
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = client.get(_OLLAMA_SETTINGS_URL, headers=headers)
        if response.status_code == 401:
            raise FetchAuthError("Ollama session cookie rejected: HTTP 401")
        response.raise_for_status()
        html = response.text
    except httpx.HTTPStatusError as exc:
        raise FetchError(
            f"Ollama usage request failed: HTTP {exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"Ollama usage request failed: {type(exc).__name__}") from exc

    debug_dump("ollama_raw.html", html)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    session = _parse_usage_block(_SESSION_LABELS, html, now)
    weekly = _parse_usage_block((_WEEKLY_LABEL,), html, now)

    # Per-model breakdown lives in the weekly bar's segment buttons.
    weekly_block = _block_after(_WEEKLY_LABEL, html)
    models = _parse_model_segments(weekly_block) if weekly_block else []

    if session is None and weekly is None:
        if _looks_signed_out(html):
            raise FetchAuthError("Ollama session cookie expired: settings page is signed out")
        raise FetchError("Ollama settings page missing usage data")

    return Reading(
        provider=Provider.OLLAMA,
        status=ReadingStatus.CURRENT,
        session_percent=session[0] if session else None,
        session_resets_at=session[1] if session else None,
        weekly_percent=weekly[0] if weekly else None,
        weekly_resets_at=weekly[1] if weekly else None,
        fetched_at=now,
        stale=False,
        models=models if models else None,
    )
