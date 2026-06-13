from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Callable

from usage_dashboard.server.db import Database
from usage_dashboard.server.fetch_claude import fetch_claude_usage, refresh_claude_token
from usage_dashboard.server.fetch_ollama import fetch_ollama_usage
from usage_dashboard.server.fetch_types import (
    FetchAuthError,
    FetchError,
    FetchRateLimitError,
)
from usage_dashboard.server.fetch_umans import fetch_umans_usage
from usage_dashboard.server.fetch_zai import fetch_zai_usage
from usage_dashboard.server.token_store import TokenStore
from usage_dashboard.shared.models import (
    Provider,
    Reading,
    make_offline_reading,
    make_stale_reading,
)

logger = logging.getLogger(__name__)


class FetchScheduler:
    def __init__(
        self,
        db: Database,
        claude_token: str | None = None,
        claude_refresh_token: str | None = None,
        claude_client_id: str | None = None,
        zai_key: str | None = None,
        ollama_cookie: str | None = None,
        umans_key: str | None = None,
        interval_seconds: int = 300,
        offline_threshold: int = 24,
        rate_limit_default_seconds: int = 300,
        token_store: TokenStore | None = None,
    ) -> None:
        self._db = db
        self._claude_token = claude_token
        self._claude_refresh_token = claude_refresh_token
        self._claude_client_id = claude_client_id
        self._token_store = token_store
        self._zai_key = zai_key
        self._ollama_cookie = ollama_cookie
        self._umans_key = umans_key
        self._interval_seconds = interval_seconds
        self._offline_threshold = offline_threshold
        self._rate_limit_default_seconds = rate_limit_default_seconds
        self._blocked_until: dict[Provider, datetime] = {}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _get_fetch_tasks(self) -> list[tuple[Provider, Callable[[], Reading]]]:
        tasks: list[tuple[Provider, Callable[[], Reading]]] = []
        if self._claude_token is not None:
            tasks.append(
                (Provider.CLAUDE, partial(fetch_claude_usage, self._claude_token))
            )
        if self._zai_key is not None:
            tasks.append(
                (Provider.ZAI, partial(fetch_zai_usage, self._zai_key))
            )
        if self._ollama_cookie is not None:
            tasks.append(
                (Provider.OLLAMA, partial(fetch_ollama_usage, self._ollama_cookie))
            )
        if self._umans_key is not None:
            tasks.append(
                (Provider.UMANS, partial(fetch_umans_usage, self._umans_key))
            )
        return tasks

    def _try_refresh_claude(self) -> bool:
        if self._claude_refresh_token is None:
            return False
        try:
            new_access, new_refresh = refresh_claude_token(
                self._claude_refresh_token,
                client_id=self._claude_client_id,
            )
            self._claude_token = new_access
            self._claude_refresh_token = new_refresh
            if self._token_store is not None:
                self._token_store.save_claude_tokens(new_access, new_refresh)
                logger.info("Claude tokens refreshed and persisted")
            else:
                logger.info("Claude token refreshed successfully (not persisted)")
            return True
        except FetchError as exc:
            logger.warning("Claude token refresh failed: %s", exc)
            return False

    def _is_blocked(self, provider: Provider, now: datetime) -> bool:
        blocked_until = self._blocked_until.get(provider)
        if blocked_until is None:
            return False
        if now >= blocked_until:
            del self._blocked_until[provider]
            return False
        return True

    def _fetch_one(
        self,
        provider: Provider,
        fetch_fn: Callable[[], Reading],
    ) -> None:
        try:
            reading = fetch_fn()
            self._db.store_reading(reading, consecutive_failures=0)
            self._db.reset_failures(provider)
        except FetchError as exc:
            # Refresh only on credential rejection; refreshing on transient
            # failures (429s, timeouts) spams the OAuth endpoint and risks
            # rotating a refresh token shared with an interactive session.
            if (
                provider == Provider.CLAUDE
                and isinstance(exc, FetchAuthError)
                and self._try_refresh_claude()
            ):
                try:
                    reading = fetch_claude_usage(self._claude_token or "")
                    self._db.store_reading(reading, consecutive_failures=0)
                    self._db.reset_failures(provider)
                    return
                except FetchError:
                    pass
            self._record_failure(provider, exc)
        except Exception as exc:
            # A non-FetchError (e.g. AttributeError parsing an unexpected API
            # payload) must be contained; if it escapes, _run_loop's thread
            # dies and every provider stops fetching for the life of the pod.
            self._record_failure(provider, exc)

    def _record_failure(self, provider: Provider, exc: Exception) -> None:
        existing = self._db.get_latest_readings().get(provider)
        failures = self._db.increment_failures(provider)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if isinstance(exc, FetchRateLimitError):
            backoff = exc.retry_after_seconds or float(self._rate_limit_default_seconds)
            self._blocked_until[provider] = now + timedelta(seconds=backoff)
            logger.info(
                "Backing off %s for %.0fs after rate limit", provider.value, backoff
            )
        if failures >= self._offline_threshold:
            self._db.store_reading(
                make_offline_reading(provider, now),
                consecutive_failures=failures,
            )
        elif existing is not None:
            self._db.store_reading(
                make_stale_reading(existing),
                consecutive_failures=failures,
            )
        else:
            self._db.store_reading(
                make_offline_reading(provider, now),
                consecutive_failures=failures,
            )
        logger.warning(
            "Fetch failed for %s: %s",
            provider.value,
            exc,
        )

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            tasks = self._get_fetch_tasks()
            for provider, fetch_fn in tasks:
                if self._stop_event.is_set():
                    return
                if self._is_blocked(
                    provider, datetime.now(timezone.utc).replace(tzinfo=None)
                ):
                    continue
                self._fetch_one(provider, fetch_fn)
            self._stop_event.wait(timeout=self._interval_seconds)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
