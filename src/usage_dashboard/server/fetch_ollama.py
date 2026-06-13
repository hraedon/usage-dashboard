from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx

from usage_dashboard.server.fetch_types import FetchAuthError, FetchError
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus

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
_BLOCK_MAX_CHARS = 4000

_PERCENT_USED_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%\s*used", re.IGNORECASE)
_BAR_WIDTH_RE = re.compile(r"width:\s*([0-9]+(?:\.[0-9]+)?)%", re.IGNORECASE)
_DATA_TIME_RE = re.compile(r'data-time="([^"]+)"')

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


def _parse_resets_at(block: str) -> datetime | None:
    match = _DATA_TIME_RE.search(block)
    if match is None:
        return None
    raw = match.group(1)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.replace(tzinfo=None)


def _parse_usage_block(labels: tuple[str, ...], html: str) -> tuple[float, datetime | None] | None:
    for label in labels:
        block = _block_after(label, html)
        if block is None:
            continue
        percent = _parse_percent(block)
        if percent is not None:
            return percent, _parse_resets_at(block)
    return None


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

    session = _parse_usage_block(_SESSION_LABELS, html)
    weekly = _parse_usage_block((_WEEKLY_LABEL,), html)

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
        fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
        stale=False,
    )
