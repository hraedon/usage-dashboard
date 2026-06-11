from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from usage_dashboard.server.fetch_types import FetchError
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus

_OLLAMA_LOGIN_URL = "https://ollama.com/api/login"
_OLLAMA_USAGE_URL = "https://ollama.com/settings/usage"
_TIMEOUT = 30.0

_SESSION_PATTERN = re.compile(r"session", re.IGNORECASE)
_WEEKLY_PATTERN = re.compile(r"weekly", re.IGNORECASE)
_PERCENT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _parse_percent(text: str) -> float | None:
    match = _PERCENT_PATTERN.search(text)
    if match:
        return float(match.group(1))
    return None


def _do_login(client: httpx.Client, email: str, password: str) -> None:
    login_response = client.post(
        _OLLAMA_LOGIN_URL,
        json={"email": email, "password": password},
    )
    login_response.raise_for_status()


def _fetch_usage_html(email: str, password: str) -> str:
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            _do_login(client, email, password)
            usage_response = client.get(_OLLAMA_USAGE_URL)
            if usage_response.status_code in (401, 403):
                _do_login(client, email, password)
                usage_response = client.get(_OLLAMA_USAGE_URL)
            usage_response.raise_for_status()
            return usage_response.text
    except httpx.HTTPError as exc:
        raise FetchError(f"Ollama usage request failed: {type(exc).__name__}") from exc


def fetch_ollama_usage(email: str, password: str) -> Reading:
    html = _fetch_usage_html(email, password)

    try:
        soup = BeautifulSoup(html, "html.parser")
        session_percent: float | None = None
        weekly_percent: float | None = None
        for element in soup.find_all(string=_SESSION_PATTERN):
            parent = element.find_parent()
            if parent:
                container = parent.find_parent()
                if container:
                    text = container.get_text(separator=" ", strip=True)
                    pct = _parse_percent(text)
                    if pct is not None:
                        session_percent = pct
                        break
        for element in soup.find_all(string=_WEEKLY_PATTERN):
            parent = element.find_parent()
            if parent:
                container = parent.find_parent()
                if container:
                    text = container.get_text(separator=" ", strip=True)
                    pct = _parse_percent(text)
                    if pct is not None:
                        weekly_percent = pct
                        break
        if session_percent is None:
            raise FetchError("Ollama usage page missing session percent")
        if weekly_percent is None:
            raise FetchError("Ollama usage page missing weekly percent")
    except FetchError:
        raise
    except (ValueError, TypeError) as exc:
        raise FetchError(f"Ollama usage page parse error: {type(exc).__name__}") from exc

    return Reading(
        provider=Provider.OLLAMA,
        status=ReadingStatus.CURRENT,
        session_percent=session_percent,
        session_resets_at=None,
        weekly_percent=weekly_percent,
        weekly_resets_at=None,
        fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
        stale=False,
    )
