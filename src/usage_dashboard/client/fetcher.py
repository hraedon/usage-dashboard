from __future__ import annotations

import logging
import threading
from typing import Any

import httpx

from usage_dashboard.shared.models import Reading

logger = logging.getLogger(__name__)


class ClientFetcher:
    def __init__(
        self,
        server_url: str,
        api_key: str,
        default_interval: int = 300,
        fast_interval: int = 60,
        stable_threshold: int = 5,
        unit_id: str | None = None,
        fetch_schedule: bool = False,
    ) -> None:
        self._server_url = server_url.rstrip("/")
        self._api_key = api_key
        self._default_interval = default_interval
        self._fast_interval = fast_interval
        self._stable_threshold = stable_threshold
        self._unit_id = unit_id
        self._fetch_schedule = fetch_schedule

        self._lock = threading.Lock()
        self._readings: list[Reading] = []
        self._schedule_spec: str | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._interval = default_interval
        self._stable_count = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def get_latest_readings(self) -> list[Reading]:
        with self._lock:
            return list(self._readings)

    @property
    def current_interval(self) -> int:
        with self._lock:
            return self._interval

    @property
    def current_schedule_spec(self) -> str | None:
        """Latest server-delivered backlight schedule spec, or None (not
        fetched yet, server has none, or the server was unreachable)."""
        with self._lock:
            return self._schedule_spec

    def _poll_loop(self) -> None:
        self._fetch_once()
        while not self._stop_event.wait(timeout=self._interval):
            self._fetch_once()

    def _fetch_once(self) -> None:
        try:
            response = httpx.get(
                f"{self._server_url}/readings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=15.0,
            )
            response.raise_for_status()
            data: list[dict[str, Any]] = response.json()
            new_readings = [Reading.from_dict(item) for item in data]
            self._update_interval(new_readings)
            with self._lock:
                self._readings = new_readings
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Failed to fetch readings: %s", exc)
        if self._fetch_schedule:
            self._poll_schedule()

    def _poll_schedule(self) -> None:
        """Refresh the backlight schedule spec from the server. On any error the
        previously-cached spec is kept (the client falls back on its own)."""
        try:
            params = {"unit": self._unit_id} if self._unit_id else {}
            response = httpx.get(
                f"{self._server_url}/schedule",
                headers={"Authorization": f"Bearer {self._api_key}"},
                params=params,
                timeout=15.0,
            )
            response.raise_for_status()
            spec = response.json().get("schedule")
            with self._lock:
                self._schedule_spec = spec if isinstance(spec, str) else None
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Failed to fetch schedule: %s", exc)

    def _update_interval(self, new_readings: list[Reading]) -> None:
        with self._lock:
            old_readings = self._readings

        if self._readings_changed(old_readings, new_readings):
            self._stable_count = 0
            self._interval = self._fast_interval
        else:
            self._stable_count += 1
            if self._stable_count >= self._stable_threshold:
                self._interval = self._default_interval

    @staticmethod
    def _readings_changed(old: list[Reading], new: list[Reading]) -> bool:
        if len(old) != len(new):
            return True
        old_by_provider = {r.provider: r for r in old}
        for r in new:
            match = old_by_provider.get(r.provider)
            if match is None:
                return True
            if (
                r.session_percent != match.session_percent
                or r.weekly_percent != match.weekly_percent
            ):
                return True
        return False
