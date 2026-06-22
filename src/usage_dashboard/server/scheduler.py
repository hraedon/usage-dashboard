from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, Callable

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
    THROTTLE_BOXED,
    Provider,
    Reading,
    make_offline_reading,
    make_stale_reading,
)

logger = logging.getLogger(__name__)

# Never sleep less than this between loop ticks (avoid a busy-loop), and never
# sleep so long that the loop stops re-evaluating due times for new providers.
_MIN_SLEEP_SECONDS = 1.0
_MAX_SLEEP_SECONDS = 1800.0


@dataclass
class _ProviderSchedule:
    """Per-provider polling state, tracked independently of every other.

    ``interval`` is the gap to the *next* poll (the frequency knob the two
    ladders move); ``next_due`` is when that poll may run; ``last_success`` is
    the freshness marker (when we last got a real reading).
    """

    interval: float
    next_due: datetime
    last_success: datetime | None = None
    failures: int = 0


class FetchScheduler:
    def __init__(
        self,
        db: Database,
        claude_token: str | None = None,
        claude_refresh_token: str | None = None,
        claude_client_id: str | None = None,
        claude_work_token: str | None = None,
        claude_work_refresh_token: str | None = None,
        claude_work_client_id: str | None = None,
        zai_key: str | None = None,
        ollama_cookie: str | None = None,
        umans_key: str | None = None,
        interval_seconds: int = 300,
        offline_threshold: int = 24,
        rate_limit_default_seconds: int = 300,
        idle_ladder_seconds: tuple[int, ...] | None = None,
        failure_cap_seconds: int = 3600,
        change_epsilon: float = 0.5,
        token_store: TokenStore | None = None,
    ) -> None:
        self._db = db
        self._claude_token = claude_token
        self._claude_refresh_token = claude_refresh_token
        self._claude_client_id = claude_client_id
        self._claude_work_token = claude_work_token
        self._claude_work_refresh_token = claude_work_refresh_token
        self._claude_work_client_id = claude_work_client_id
        self._token_store = token_store
        self._zai_key = zai_key
        self._ollama_cookie = ollama_cookie
        self._umans_key = umans_key
        self._offline_threshold = offline_threshold
        self._rate_limit_default_seconds = rate_limit_default_seconds
        # The idle ladder widens the poll gap when a provider's reading is not
        # changing, to cut baseline usage. It defaults to 5 -> 10 -> 15 -> 30
        # minutes derived from the configured floor (interval_seconds).
        floor = interval_seconds
        self._idle_ladder: tuple[float, ...] = (
            tuple(float(s) for s in idle_ladder_seconds)
            if idle_ladder_seconds
            else (float(floor), float(floor * 2), float(floor * 3), float(floor * 6))
        )
        self._poll_floor = self._idle_ladder[0]
        self._failure_cap = float(failure_cap_seconds)
        self._change_epsilon = change_epsilon
        self._schedules: dict[Provider, _ProviderSchedule] = {}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # -- scheduling state ---------------------------------------------------

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def _schedule(self, provider: Provider) -> _ProviderSchedule:
        """Return the provider's schedule, creating it due-now on first sight."""
        sched = self._schedules.get(provider)
        if sched is None:
            sched = _ProviderSchedule(interval=self._poll_floor, next_due=self._now())
            self._schedules[provider] = sched
        return sched

    def _is_blocked(self, provider: Provider, now: datetime) -> bool:
        """True if the provider is not yet due (read-only; no side effects)."""
        sched = self._schedules.get(provider)
        if sched is None:
            return False
        return now < sched.next_due

    def schedule_snapshot(self) -> dict[Provider, dict[str, Any]]:
        """Observability view of per-provider frequency and freshness."""
        return {
            provider: {
                "interval_seconds": sched.interval,
                "next_due": sched.next_due,
                "last_success": sched.last_success,
                "failures": sched.failures,
            }
            for provider, sched in self._schedules.items()
        }

    def _next_idle_interval(self, current: float) -> float:
        """The next rung of the idle ladder strictly above *current*."""
        for rung in self._idle_ladder:
            if rung > current:
                return rung
        return self._idle_ladder[-1]

    def _reading_changed(self, prev: Reading, new: Reading) -> bool:
        """True if *new* differs from *prev* enough to warrant fast polling.

        Drives the idle ladder: an unchanged reading lets the gap widen, a
        changed one snaps it back to the floor. Percentages compare with an
        epsilon so background jitter doesn't keep a provider pinned at 5m;
        ``detail`` is compared verbatim because quota-less providers (umans)
        carry their movement there rather than in the percent fields.
        """
        if prev.status is not new.status:
            return True
        if (prev.detail or "") != (new.detail or ""):
            return True
        pairs = (
            (prev.session_percent, new.session_percent),
            (prev.weekly_percent, new.weekly_percent),
        )
        for old, cur in pairs:
            if (old is None) != (cur is None):
                return True
            if old is not None and cur is not None and abs(old - cur) >= self._change_epsilon:
                return True
        if prev.session_resets_at != new.session_resets_at:
            return True
        if prev.weekly_resets_at != new.weekly_resets_at:
            return True
        return False

    def _schedule_after_success(
        self, provider: Provider, previous: Reading | None, reading: Reading
    ) -> None:
        sched = self._schedule(provider)
        recovered = previous is None or previous.stale
        if reading.throttle == THROTTLE_BOXED:
            # While the account is in the penalty box the reading is unchanged
            # poll-to-poll (boxed_until is fixed), which would let the idle
            # ladder widen the gap to ~30m. Pin to the floor instead so the box
            # clearing is caught on the next scan and the metrics return promptly.
            new_interval = self._poll_floor
            reason = "boxed"
        elif recovered or self._reading_changed(previous, reading):  # type: ignore[arg-type]
            new_interval = self._poll_floor
            reason = "recovered" if recovered else "changed"
        else:
            new_interval = self._next_idle_interval(sched.interval)
            reason = "idle"
        if new_interval != sched.interval:
            logger.info(
                "%s poll interval -> %.0fs (%s)", provider.value, new_interval, reason
            )
        sched.interval = new_interval
        sched.failures = 0
        sched.last_success = reading.fetched_at
        sched.next_due = self._now() + timedelta(seconds=new_interval)

    # -- fetch tasks --------------------------------------------------------

    def configured_providers(self) -> list[Provider]:
        """Providers that actually have credentials, in ``Provider`` order.

        This is the set the API should report on: a provider absent here was
        never configured (no credential), which is different from a configured
        provider that is currently failing/offline. Derived from
        ``_get_fetch_tasks`` so the two can never drift (WI-003).
        """
        return [provider for provider, _ in self._get_fetch_tasks()]

    def _get_fetch_tasks(self) -> list[tuple[Provider, Callable[[], Reading]]]:
        tasks: list[tuple[Provider, Callable[[], Reading]]] = []
        if self._claude_token is not None:
            tasks.append(
                (Provider.CLAUDE, partial(fetch_claude_usage, self._claude_token))
            )
        if self._claude_work_token is not None:
            tasks.append(
                (Provider.CLAUDE_WORK, partial(fetch_claude_usage, self._claude_work_token))
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

    def _do_refresh(
        self, refresh_token: str, client_id: str | None, store_key: str
    ) -> tuple[str, str] | None:
        """Refresh one Claude account's tokens and persist them under
        *store_key*. Returns the new (access, refresh) pair, or None on failure.
        """
        try:
            new_access, new_refresh = refresh_claude_token(
                refresh_token, client_id=client_id
            )
        except FetchError as exc:
            logger.warning("Claude token refresh failed (%s): %s", store_key, exc)
            return None
        if self._token_store is not None:
            self._token_store.save(store_key, new_access, new_refresh)
            logger.info("Claude tokens refreshed and persisted (%s)", store_key)
        else:
            logger.info("Claude token refreshed, not persisted (%s)", store_key)
        return new_access, new_refresh

    def _try_refresh_claude(self) -> bool:
        if self._claude_refresh_token is None:
            return False
        pair = self._do_refresh(self._claude_refresh_token, self._claude_client_id, "claude")
        if pair is None:
            return False
        self._claude_token, self._claude_refresh_token = pair
        return True

    def _try_refresh_claude_work(self) -> bool:
        if self._claude_work_refresh_token is None:
            return False
        pair = self._do_refresh(
            self._claude_work_refresh_token, self._claude_work_client_id, "claude_work"
        )
        if pair is None:
            return False
        self._claude_work_token, self._claude_work_refresh_token = pair
        return True

    def _fetch_one(
        self,
        provider: Provider,
        fetch_fn: Callable[[], Reading],
    ) -> None:
        previous = self._db.get_latest_readings().get(provider)
        try:
            reading = fetch_fn()
            self._db.store_reading(reading, consecutive_failures=0)
            self._db.reset_failures(provider)
            self._schedule_after_success(provider, previous, reading)
        except FetchError as exc:
            # Refresh only on credential rejection; refreshing on transient
            # failures (429s, timeouts) spams the OAuth endpoint and risks
            # rotating a refresh token shared with an interactive session. Each
            # Claude account refreshes and retries with its own tokens.
            if isinstance(exc, FetchAuthError):
                if provider == Provider.CLAUDE and self._try_refresh_claude():
                    if self._retry_claude(provider, previous, self._claude_token):
                        return
                elif provider == Provider.CLAUDE_WORK and self._try_refresh_claude_work():
                    if self._retry_claude(provider, previous, self._claude_work_token):
                        return
            self._record_failure(provider, exc)
        except Exception as exc:
            # A non-FetchError (e.g. AttributeError parsing an unexpected API
            # payload) must be contained; if it escapes, _run_loop's thread
            # dies and every provider stops fetching for the life of the pod.
            self._record_failure(provider, exc)

    def _retry_claude(
        self, provider: Provider, previous: Reading | None, token: str | None
    ) -> bool:
        """Re-fetch a Claude account after a token refresh. Returns True on
        success (reading stored, schedule advanced), False to fall through to
        the normal failure path."""
        try:
            reading = fetch_claude_usage(token or "")
        except FetchError:
            return False
        self._db.store_reading(reading, consecutive_failures=0)
        self._db.reset_failures(provider)
        self._schedule_after_success(provider, previous, reading)
        return True

    def _failure_backoff(self, provider: Provider, failures: int, exc: Exception) -> float:
        """Seconds to wait before the next attempt after *failures* in a row.

        Any failure backs off exponentially (floor, 2x, 4x, ...) capped at
        ``failure_cap``; a 429 additionally respects the server's Retry-After
        (authoritative, allowed to exceed the cap) or the rate-limit default.
        """
        backoff: float = min(self._poll_floor * float(2 ** (failures - 1)), self._failure_cap)
        if isinstance(exc, FetchRateLimitError):
            if exc.retry_after_seconds:
                backoff = max(backoff, float(exc.retry_after_seconds))
            else:
                backoff = max(backoff, float(self._rate_limit_default_seconds))
        return backoff

    def _record_failure(self, provider: Provider, exc: Exception) -> None:
        existing = self._db.get_latest_readings().get(provider)
        failures = self._db.increment_failures(provider)
        now = self._now()

        backoff = self._failure_backoff(provider, failures, exc)
        sched = self._schedule(provider)
        sched.failures = failures
        sched.interval = backoff
        sched.next_due = now + timedelta(seconds=backoff)

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
            "Fetch failed for %s (failure #%d, backing off %.0fs): %s",
            provider.value,
            failures,
            backoff,
            exc,
        )

    # -- loop ---------------------------------------------------------------

    def _seconds_until_next_due(
        self, tasks: list[tuple[Provider, Callable[[], Reading]]]
    ) -> float:
        if not tasks:
            return self._poll_floor
        now = self._now()
        earliest = min(self._schedule(provider).next_due for provider, _ in tasks)
        secs = (earliest - now).total_seconds()
        return max(_MIN_SLEEP_SECONDS, min(secs, _MAX_SLEEP_SECONDS))

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            tasks = self._get_fetch_tasks()
            now = self._now()
            for provider, fetch_fn in tasks:
                if self._stop_event.is_set():
                    return
                if now >= self._schedule(provider).next_due:
                    self._fetch_one(provider, fetch_fn)
            self._stop_event.wait(timeout=self._seconds_until_next_due(tasks))

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
