from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from functools import partial
from typing import Callable

from usage_dashboard.server.db import Database
from usage_dashboard.server.fetch_claude import fetch_claude_usage, refresh_claude_token
from usage_dashboard.server.fetch_ollama import fetch_ollama_usage
from usage_dashboard.server.fetch_types import FetchError
from usage_dashboard.server.fetch_umans import fetch_umans_usage
from usage_dashboard.server.fetch_zai import fetch_zai_usage
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
        ollama_email: str | None = None,
        ollama_password: str | None = None,
        umans_key: str | None = None,
        interval_seconds: int = 300,
        offline_threshold: int = 24,
    ) -> None:
        self._db = db
        self._claude_token = claude_token
        self._claude_refresh_token = claude_refresh_token
        self._claude_client_id = claude_client_id
        self._zai_key = zai_key
        self._ollama_email = ollama_email
        self._ollama_password = ollama_password
        self._umans_key = umans_key
        self._interval_seconds = interval_seconds
        self._offline_threshold = offline_threshold
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
        if self._ollama_email is not None and self._ollama_password is not None:
            tasks.append(
                (
                    Provider.OLLAMA,
                    partial(
                        fetch_ollama_usage,
                        self._ollama_email,
                        self._ollama_password,
                    ),
                )
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
            logger.info("Claude token refreshed successfully")
            return True
        except FetchError as exc:
            logger.warning("Claude token refresh failed: %s", exc)
            return False

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
            if provider == Provider.CLAUDE and self._try_refresh_claude():
                try:
                    reading = fetch_claude_usage(self._claude_token or "")
                    self._db.store_reading(reading, consecutive_failures=0)
                    self._db.reset_failures(provider)
                    return
                except FetchError:
                    pass

            existing = self._db.get_latest_readings().get(provider)
            failures = self._db.increment_failures(provider)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
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
