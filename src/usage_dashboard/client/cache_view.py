"""The GUI's side of the cellular readings worker (Plan 010).

The GUI and the readings worker never talk to each other directly, and this
class is why they do not need to. It is the GUI's *read-only* view of what the
worker publishes (the readings cache and its status) plus the only two signals
the GUI is allowed to send: "the panel is lit", and "please read again now".

Everything here is deliberately free of pygame and of network access, so the
decisions are unit-tested without a display:

* :meth:`select` — which readings to draw. The GUI's own fetcher and the
  worker both poll on the normal route; the newer successful read wins. That
  single rule gives fallback for free: when the normal route dies the worker's
  cellular reads are the only thing getting newer, and after a restart with
  both routes down the preserved payload is shown with its age.
* :meth:`mark_awake` / :meth:`mark_dark` — the heartbeat the worker spends
  data against. Refreshed at most every half minute while lit, removed as soon
  as the panel goes dark, so a sleeping panel costs nothing.
* :meth:`request_reread` / :meth:`await_reread` — the refresh button on the
  cellular path. It cannot ask the server for a fresh collection (the link
  permits one GET), so it asks the worker to re-read the stored snapshot.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from usage_dashboard.client import readings_cache as cache
from usage_dashboard.shared.models import Reading

logger = logging.getLogger(__name__)

# What the status line calls the metered route.
SOURCE_LABEL = "cellular"

# The heartbeat is refreshed this often while the panel is lit. It must be far
# shorter than the worker's CELLULAR_AWAKE_TIMEOUT (150 s by default).
AWAKE_EVERY_SECONDS = 30.0
# How long a refresh tap waits for the worker to answer before the panel says
# the attempt failed. The worker's normal-route probe can take a few seconds to
# time out before it even starts the cellular read.
REREAD_WAIT_SECONDS = 45.0
# A cache entry stamped further ahead than this is not trusted. The worker
# stamps entries with the unit's clock; one in the future means the clock has
# stepped backwards since (a Pi without a real-time clock can come up ahead as
# easily as behind until it synchronises). Preferred on its timestamp alone,
# such an entry would outrank every newer reading until real time caught up
# with it — days, if the step was days.
FUTURE_TOLERANCE_SECONDS = 300.0

REFRESHED = "refreshed"
DEFERRED = "deferred"
FAILED = "failed"


@dataclass(frozen=True)
class Selection:
    """What the panel should draw this frame."""

    readings: list[Reading]
    # When these readings were last read from the server (naive UTC), or None
    # while there is nothing to show.
    last_success_at: datetime | None
    # True when the metered link carried them.
    cellular: bool
    # The cadence the status line should print when it is not the GUI's own.
    poll_interval: int | None


class CacheView:
    def __init__(
        self,
        state_dir: Path,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._state_dir = state_dir
        self._now = now
        self._request_dir = state_dir / cache.REQUEST_DIR
        self._reader = cache.CacheReader(state_dir)
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_touch: float | None = None
        self._dark = False

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    @property
    def request_dir(self) -> Path:
        return self._request_dir

    # -- what to draw ----------------------------------------------------------

    def select(self, own: list[Reading], own_last_success: datetime | None) -> Selection:
        """The readings to draw: the GUI's own, or the worker's cache if that
        holds a newer successful read.

        Both timestamps are naive UTC. With no successful read of its own yet
        — the first frames after a start — the cache wins whenever there is
        one, which is what shows the preserved payload after a restart.
        """
        entry = self._reader.latest()
        if entry is not None and self._stamped_ahead(entry):
            logger.warning(
                "Ignoring a readings cache stamped %s, which is ahead of the clock",
                entry.written_at.isoformat(),
            )
            entry = None
        newer_own = (
            entry is None
            or (own_last_success is not None and own_last_success >= entry.written_at)
        )
        if newer_own:
            return Selection(own, own_last_success, False, None)
        assert entry is not None
        try:
            readings = entry.readings()
        except cache.CacheFormatError as exc:
            # decode_cache already refused such a file; this can only be a
            # race with a writer that is being replaced mid-upgrade.
            logger.warning("Ignoring the readings cache: %s", exc)
            return Selection(own, own_last_success, False, None)
        cellular = entry.source == cache.SOURCE_CELLULAR
        # On the normal route the GUI's own poller is what will refresh the
        # panel next, so its interval is still the honest one to print.
        interval = entry.poll_interval if cellular else None
        return Selection(readings, entry.written_at, cellular, interval)

    def _stamped_ahead(self, entry: cache.CacheEntry) -> bool:
        """Whether *entry* claims to be newer than now by more than clock noise."""
        now = datetime.fromtimestamp(self._now(), timezone.utc).replace(tzinfo=None)
        return entry.written_at > now + timedelta(seconds=FUTURE_TOLERANCE_SECONDS)

    # -- what the panel is doing ------------------------------------------------

    def mark_awake(self) -> None:
        """Record that the panel is lit. Cheap to call every frame."""
        now = self._monotonic()
        if (
            not self._dark
            and self._last_touch is not None
            and now - self._last_touch < AWAKE_EVERY_SECONDS
        ):
            return
        self._dark = False
        self._last_touch = now
        cache.touch_awake(self._request_dir)

    def mark_dark(self) -> None:
        """Record that the panel is dark, once per transition, so the worker
        stops spending data immediately rather than when the heartbeat ages out."""
        if self._dark:
            return
        self._dark = True
        self._last_touch = None
        cache.clear_awake(self._request_dir)

    # -- the refresh button ---------------------------------------------------------

    def request_reread(self) -> float:
        """Ask the worker to read the snapshot again now. Returns the token
        that identifies this request for :meth:`await_reread`."""
        token = time.time()
        if not cache.request_refresh(self._request_dir, token):
            logger.warning("Could not signal the cellular worker to re-read")
        return token

    def await_reread(self, token: float, before: datetime | None) -> str:
        """Wait for the result of :meth:`request_reread`.

        ``before`` is the timestamp of the payload on screen. The call returns
        as soon as the cache holds something newer (``refreshed``), as soon as
        the worker says it declined — a tap inside the minimum gap between
        cellular reads (``deferred``), or when nothing has happened in time
        (``failed``).
        """
        deadline = self._monotonic() + REREAD_WAIT_SECONDS
        while True:
            entry = self._reader.latest(force=True)
            if entry is not None and (before is None or entry.written_at > before):
                return REFRESHED
            status = self.status()
            if (
                status is not None
                and status.request is not None
                and status.request.token == token
                and status.request.result == "deferred"
            ):
                return DEFERRED
            if self._monotonic() >= deadline:
                return FAILED
            self._sleep(0.5)

    def status(self) -> cache.WorkerStatus | None:
        """The worker's last published status, for the diagnostics overlay."""
        return cache.read_status(self._state_dir)
