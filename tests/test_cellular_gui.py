"""What the panel does with the cellular readings worker's output (Plan 010).

The worker is the only thing that may use the metered link, and the GUI is the
reason it exists — so the GUI's side of the arrangement is small but has to be
exactly right in three places:

* **what to draw** — the newer of the GUI's own readings and the worker's
  cache. That one rule is the fallback: on a dead normal route the worker's
  cellular reads are the only thing getting newer, and after a restart with
  both routes down the preserved payload is shown *with its age*;
* **when the worker may spend data** — only while the panel is lit, which the
  GUI tells it through one heartbeat file;
* **the refresh button** — on cellular it cannot ask for a fresh collection,
  so it asks for a re-read and says plainly whether that happened, was
  declined, or failed.

The decisions live in :mod:`usage_dashboard.client.cache_view` and are tested
without a display; the last section runs the real pygame layer under SDL's
dummy driver to show the panel is wired to them.
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from usage_dashboard.client import cache_view, layout
from usage_dashboard.client import diagnostics as diag
from usage_dashboard.client import format as fmt
from usage_dashboard.client import readings_cache as cache
from usage_dashboard.client.cache_view import (
    DEFERRED,
    FAILED,
    REFRESHED,
    SOURCE_LABEL,
    CacheView,
)
from usage_dashboard.client.fetcher import ClientFetcher
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

_NOW = datetime(2026, 11, 20, 9, 30, 0)  # naive UTC
_LATER = _NOW + timedelta(minutes=5)


def _reading(provider: Provider = Provider.CLAUDE, percent: float = 40.0) -> Reading:
    return Reading(
        provider, ReadingStatus.CURRENT, percent, _NOW, 55.0, _NOW, _NOW, False,
    )


def _payload(*readings: Reading) -> list[dict[str, Any]]:
    return [r.to_dict() for r in readings]


# -- the status line ----------------------------------------------------------------


class TestStatusLine:
    def test_unchanged_without_an_age_or_a_route(self) -> None:
        text = layout.build_main_layout([_reading()], (480, 320), refresh_interval=300).status_text
        assert text.endswith(" · refresh 5m · 1 provider")
        assert "ago" not in text and SOURCE_LABEL not in text

    def test_the_route_is_named_when_it_is_not_the_normal_one(self) -> None:
        text = layout.build_main_layout(
            [_reading()], (480, 320), refresh_interval=600, source_label=SOURCE_LABEL,
        ).status_text
        clock = fmt.to_local(_NOW).strftime("%H:%M:%S")
        assert text == f"Updated {clock} · cellular · refresh 10m · 1 provider"

    @pytest.mark.parametrize(
        "interval,shown_from",
        [(None, 600), (60, 600), (300, 600), (600, 1200), (3600, 7200)],
    )
    def test_age_appears_once_it_outruns_the_promised_cadence(
        self, interval: int | None, shown_from: int
    ) -> None:
        def text_for(age: float) -> str:
            return layout.build_main_layout(
                [_reading()], (480, 320), refresh_interval=interval, age_seconds=age,
            ).status_text

        assert "ago" not in text_for(shown_from - 1)
        assert "ago" in text_for(shown_from)
        # A healthy panel on its normal cadence never flickers an age on and off.
        assert "ago" not in text_for(299)

    def test_the_age_reads_next_to_the_time_of_the_reading(self) -> None:
        text = layout.build_main_layout(
            [_reading()], (1280, 720), refresh_interval=600, age_seconds=3 * 3600 + 5 * 60,
            source_label=SOURCE_LABEL,
        ).status_text
        assert " · cellular · 3h 5m ago · refresh 10m · 1 provider" in text

    def test_nothing_to_show_still_says_so(self) -> None:
        text = layout.build_main_layout([], (480, 320), age_seconds=9999).status_text
        assert text == "Waiting for data…"

    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "0m ago"), (59, "0m ago"), (60, "1m ago"), (12 * 60, "12m ago"),
            (3600, "1h 0m ago"), (23 * 3600 + 5 * 60, "23h 5m ago"),
            (86400, "1d 0h ago"), (3 * 86400 + 2 * 3600, "3d 2h ago"), (-5, "0m ago"),
        ],
    )
    def test_format_age(self, seconds: float, expected: str) -> None:
        assert fmt.format_age(seconds) == expected


# -- the fetcher's own "last read" time ----------------------------------------------


class TestFetcherLastSuccess:
    def _fetcher(self) -> ClientFetcher:
        return ClientFetcher("https://dash.example", "k", default_interval=300)

    def test_it_is_none_until_a_read_succeeds(self) -> None:
        fetcher = self._fetcher()
        assert fetcher.last_success_at is None
        fetcher._fetch_once()
        assert fetcher.last_success_at is None        # nothing answered

    def test_it_records_the_read_not_the_server_fetch(self) -> None:
        fetcher = self._fetcher()
        before = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
        with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_payload(_reading()), request=request)
        )) as client:
            patched = httpx.get
            httpx.get = client.get  # type: ignore[assignment]
            try:
                fetcher._fetch_once()
            finally:
                httpx.get = patched  # type: ignore[assignment]
        success = fetcher.last_success_at
        assert success is not None and before <= success <= (
            datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=1)
        )
        assert success.tzinfo is None
        # The server fetched its reading long before the Pi read it.
        assert success > fetcher.get_latest_readings()[0].fetched_at

    def test_a_failed_read_leaves_the_last_success_alone(self) -> None:
        fetcher = self._fetcher()
        fetcher._last_success_at = _NOW
        fetcher._fetch_once()
        assert fetcher.last_success_at == _NOW


# -- which readings to draw ------------------------------------------------------------


class TestSelection:
    def _view(self, tmp_path: Path, **kwargs: Any) -> CacheView:
        return CacheView(tmp_path, **kwargs)

    def _publish(self, tmp_path: Path, written_at: datetime, source: str) -> None:
        cache.publish(
            cache.CacheEntry(written_at, source, 600, _payload(_reading(percent=61.0))),
            tmp_path,
        )

    def test_with_no_cache_the_gui_draws_its_own(self, tmp_path: Path) -> None:
        own = [_reading()]
        selection = self._view(tmp_path).select(own, _NOW)
        assert selection.readings == own
        assert (selection.last_success_at, selection.cellular, selection.poll_interval) == (
            _NOW, False, None,
        )

    def test_after_a_start_the_cache_is_drawn_before_the_first_own_read(
        self, tmp_path: Path
    ) -> None:
        # Both routes down, the panel has just restarted: the preserved payload
        # is what there is, and its timestamp is what the age is computed from.
        self._publish(tmp_path, _NOW - timedelta(hours=3), cache.SOURCE_CELLULAR)
        selection = self._view(tmp_path).select([], None)
        assert [r.session_percent for r in selection.readings] == [61.0]
        assert selection.last_success_at == _NOW - timedelta(hours=3)
        assert selection.cellular is True and selection.poll_interval == 600

    def test_the_newer_read_wins_whichever_side_it_came_from(self, tmp_path: Path) -> None:
        own = [_reading(percent=40.0)]
        self._publish(tmp_path, _LATER, cache.SOURCE_CELLULAR)
        assert self._view(tmp_path).select(own, _NOW).readings[0].session_percent == 61.0
        assert self._view(tmp_path).select(own, _LATER).readings == own        # a tie is the GUI's
        assert self._view(tmp_path).select(own, _LATER + timedelta(seconds=1)).readings == own

    def test_a_cache_from_the_normal_route_is_not_marked_cellular(self, tmp_path: Path) -> None:
        self._publish(tmp_path, _LATER, cache.SOURCE_NETWORK)
        selection = self._view(tmp_path).select([_reading()], _NOW)
        assert selection.cellular is False and selection.poll_interval is None
        # ...and it is still drawn, because it is the newer read.
        assert selection.readings[0].session_percent == 61.0

    def test_a_damaged_cache_never_replaces_good_readings(self, tmp_path: Path) -> None:
        (tmp_path / cache.READINGS_FILE).write_text("{not json")
        own = [_reading()]
        assert self._view(tmp_path).select(own, _NOW).readings == own

    def test_the_cache_is_not_reread_until_it_changes(self, tmp_path: Path) -> None:
        self._publish(tmp_path, _LATER, cache.SOURCE_CELLULAR)
        view = self._view(tmp_path)
        first = view.select([], None)
        assert view.select([], None) == first
        self._publish(tmp_path, _LATER + timedelta(minutes=10), cache.SOURCE_NETWORK)
        later = view.select([], None)
        assert later != first and later.cellular is False


# -- the heartbeat ----------------------------------------------------------------------


class TestHeartbeat:
    def _clock(self) -> list[float]:
        return [1000.0]

    def _view(self, tmp_path: Path, clock: list[float]) -> CacheView:
        return CacheView(tmp_path, monotonic=lambda: clock[0])

    def test_a_lit_panel_refreshes_it_every_half_minute(self, tmp_path: Path) -> None:
        clock = self._clock()
        view = self._view(tmp_path, clock)
        awake = tmp_path / cache.REQUEST_DIR / cache.AWAKE_FILE
        view.mark_awake()
        assert awake.exists()
        first = awake.stat().st_mtime_ns
        clock[0] += 10
        view.mark_awake()
        assert awake.stat().st_mtime_ns == first          # too soon: not rewritten
        clock[0] += 21
        view.mark_awake()
        assert awake.stat().st_mtime_ns != first
        # The interval must sit well inside the worker's freshness window, or a
        # healthy panel would look dark to it.
        assert cache_view.AWAKE_EVERY_SECONDS < 150 / 3

    def test_a_dark_panel_removes_it_at_once_and_only_once(self, tmp_path: Path) -> None:
        clock = self._clock()
        view = self._view(tmp_path, clock)
        view.mark_awake()
        awake = tmp_path / cache.REQUEST_DIR / cache.AWAKE_FILE
        removes = []
        real_unlink = Path.unlink

        def counted(self: Path, missing_ok: bool = False) -> None:
            removes.append(1)
            real_unlink(self, missing_ok=missing_ok)

        Path.unlink = counted  # type: ignore[assignment]
        try:
            view.mark_dark()
            view.mark_dark()      # called every frame while dark
            view.mark_dark()
        finally:
            Path.unlink = real_unlink  # type: ignore[assignment]
        assert not awake.exists() and len(removes) == 1

    def test_waking_refreshes_it_immediately(self, tmp_path: Path) -> None:
        clock = self._clock()
        view = self._view(tmp_path, clock)
        view.mark_awake()
        clock[0] += 5
        view.mark_dark()
        view.mark_awake()         # the tap that woke the panel
        awake = tmp_path / cache.REQUEST_DIR / cache.AWAKE_FILE
        # The file's stamp is the real clock's; only the throttle uses the fake.
        assert awake.exists() and cache.is_awake(awake.parent, time.time(), 150)


# -- the refresh button ------------------------------------------------------------------


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class TestRefresh:
    def _view(self, tmp_path: Path, clock: _FakeClock) -> CacheView:
        # Through lambdas, so a test may replace the clock's sleep afterwards.
        return CacheView(
            tmp_path, monotonic=lambda: clock.monotonic(), sleep=lambda s: clock.sleep(s)
        )

    def test_the_request_is_a_file_the_worker_reads(self, tmp_path: Path) -> None:
        view = self._view(tmp_path, _FakeClock())
        token = view.request_reread()
        assert cache.read_refresh_token(tmp_path / cache.REQUEST_DIR) == token

    def _status_with_request(self, tmp_path: Path, token: float, result: str) -> None:
        cache.write_status(_status(request=cache.RequestOutcome(token, result)), tmp_path)

    def _publish_now(self, tmp_path: Path) -> None:
        entry = cache.CacheEntry(_NOW, cache.SOURCE_CELLULAR, 600, _payload(_reading()))
        cache.publish(entry, tmp_path)

    def test_a_newer_payload_means_refreshed(self, tmp_path: Path) -> None:
        clock = _FakeClock()
        view = self._view(tmp_path, clock)
        self._publish_now(tmp_path)
        token = view.request_reread()

        def the_worker_answers(seconds: float) -> None:
            clock.sleeps.append(seconds)
            cache.publish(
                cache.CacheEntry(
                    _LATER, cache.SOURCE_CELLULAR, 600, _payload(_reading(percent=44.0))
                ),
                tmp_path,
            )

        clock.sleep = the_worker_answers  # type: ignore[method-assign]
        assert view.await_reread(token, _NOW) == REFRESHED
        assert clock.sleeps == [0.5]

    def test_a_tap_inside_the_minimum_gap_is_deferred_not_failed(self, tmp_path: Path) -> None:
        clock = _FakeClock()
        view = self._view(tmp_path, clock)
        self._publish_now(tmp_path)
        token = view.request_reread()
        self._status_with_request(tmp_path, token, "deferred")
        clock.now += 2
        assert view.await_reread(token, _NOW) == DEFERRED

    def test_nothing_happening_is_a_failure_after_the_wait(self, tmp_path: Path) -> None:
        clock = _FakeClock()
        view = self._view(tmp_path, clock)
        self._publish_now(tmp_path)
        assert view.await_reread(view.request_reread(), _NOW) == FAILED
        assert clock.now >= 1000.0 + cache_view.REREAD_WAIT_SECONDS

    def test_a_stale_deferral_for_an_earlier_tap_does_not_count(self, tmp_path: Path) -> None:
        clock = _FakeClock()
        view = self._view(tmp_path, clock)
        self._publish_now(tmp_path)
        self._status_with_request(tmp_path, token=1.0, result="deferred")
        token = view.request_reread()
        assert token > 1.0
        clock.now += 2
        # Falls through to the normal wait, which here times out quickly.
        clock.now += cache_view.REREAD_WAIT_SECONDS
        assert view.await_reread(token, _NOW) == FAILED

    def test_an_older_payload_does_not_count_as_new(self, tmp_path: Path) -> None:
        clock = _FakeClock()
        view = self._view(tmp_path, clock)
        token = view.request_reread()

        def republishes_old(seconds: float) -> None:
            clock.sleeps.append(seconds)
            old = cache.CacheEntry(
                _NOW - timedelta(hours=1), cache.SOURCE_CELLULAR, 600, []
            )
            cache.publish(old, tmp_path)
            clock.now += cache_view.REREAD_WAIT_SECONDS

        clock.sleep = republishes_old  # type: ignore[method-assign]
        assert view.await_reread(token, _NOW) == FAILED

    def test_status_is_read_for_the_overlay(self, tmp_path: Path) -> None:
        view = self._view(tmp_path, _FakeClock())
        assert view.status() is None
        self._status_with_request(tmp_path, 1.0, "polled")
        status = view.status()
        assert status is not None and status.request is not None


# -- the diagnostics overlay ------------------------------------------------------------------


def _status(**overrides: Any) -> cache.WorkerStatus:
    base: dict[str, Any] = dict(
        updated_at=_NOW, state=cache.STATE_IDLE, next_poll_at=_NOW + timedelta(minutes=2),
        poll_interval=600, last_ok_at=_NOW, last_source=cache.SOURCE_NETWORK,
        normal_error="", cellular_error="", month="2026-11", bytes_used=0,
        budget_bytes=50_000_000,
    )
    base.update(overrides)
    return cache.WorkerStatus(**base)


class TestCellularDiagnostics:
    def test_an_ordinary_healthy_unit_shows_nothing_extra(self) -> None:
        assert diag.cellular_lines(_status(), _NOW) == []
        assert diag.cellular_lines(None, _NOW) == []
        # ...so the overlay looks exactly as it did before Plan 010.
        base: dict[str, Any] = dict(
            hostname="pi", addresses=["192.168.1.5"], server_host="dash", running_commit="a1b2c3d4",
            check=None, change=None,
        )
        assert diag.diagnostic_lines(diag.Diagnostics(**base), _NOW) == diag.diagnostic_lines(
            diag.Diagnostics(**base, cellular=_status()), _NOW
        )

    def test_on_cellular_it_says_so_and_how_old_the_reading_is(self) -> None:
        lines = diag.cellular_lines(
            _status(state=cache.STATE_FALLBACK, last_source=cache.SOURCE_CELLULAR,
                    last_ok_at=_NOW - timedelta(minutes=3), normal_error="timeout"),
            _NOW,
        )
        assert lines == [diag.DiagLine("Cellular", "cellular · 3m ago (timeout)", warn=True)]

    def test_fallback_before_the_first_cellular_read(self) -> None:
        lines = diag.cellular_lines(
            _status(state=cache.STATE_FALLBACK, last_source=None, last_ok_at=None,
                    cellular_error="link_absent"),
            _NOW,
        )
        assert lines[0].value == "cellular · no reading yet (link_absent)"
        assert lines[0].warn is True

    def test_the_month_s_data_is_shown_once_any_is_used(self) -> None:
        lines = diag.cellular_lines(
            _status(state=cache.STATE_FALLBACK, last_source=cache.SOURCE_CELLULAR,
                    bytes_used=12_345_678),
            _NOW,
        )
        assert lines[1] == diag.DiagLine("Data", "12.3 of 50 MB", warn=False)
        lines = diag.cellular_lines(_status(bytes_used=47_500_000), _NOW)
        assert lines == [diag.DiagLine("Cellular", "normal route", warn=False),
                         diag.DiagLine("Data", "47.5 of 50 MB", warn=True)]

    def test_a_spent_budget_and_an_unset_clock_are_warnings(self) -> None:
        budget = diag.cellular_lines(_status(state=cache.STATE_BUDGET, bytes_used=50_100_000), _NOW)
        assert budget[0] == diag.DiagLine("Cellular", "budget used", warn=True)
        unset = _status(state=cache.STATE_CLOCK, normal_error="tls_verify")
        clock = diag.cellular_lines(unset, _NOW)
        assert clock[0] == diag.DiagLine("Cellular", "clock not set (tls_verify)", warn=True)

    def test_a_bad_display_credential_is_a_warning_on_an_otherwise_healthy_unit(self) -> None:
        lines = diag.cellular_lines(_status(normal_error="http_401"), _NOW)
        assert lines == [diag.DiagLine("Cellular", "normal route (http_401)", warn=True)]

    def test_a_worker_that_stopped_publishing_is_called_out(self) -> None:
        due = _NOW + timedelta(minutes=2)
        late = due + timedelta(minutes=5, seconds=1)
        status = _status(state=cache.STATE_FALLBACK, next_poll_at=due)
        lines = diag.cellular_lines(status, late)
        assert lines[0].value == "cellular · worker stalled" and lines[0].warn is True
        # Inside the grace period it is merely waiting, as it said it would.
        assert "stalled" not in diag.cellular_lines(status, due + timedelta(minutes=4))[0].value
        fresh = diag.cellular_lines(
            _status(state=cache.STATE_FALLBACK, next_poll_at=_NOW + timedelta(hours=1)), _NOW
        )
        assert "stalled" not in fresh[0].value

    def test_an_aware_now_is_handled(self) -> None:
        aware = _NOW.replace(tzinfo=timezone.utc)
        lines = diag.cellular_lines(_status(state=cache.STATE_FALLBACK), aware)
        assert lines[0].value == "cellular"

    def test_gather_reads_the_workers_status_when_asked(self, tmp_path: Path) -> None:
        cellular_dir = tmp_path / "cellular"
        cellular_dir.mkdir()
        cache.write_status(_status(state=cache.STATE_BUDGET), cellular_dir)
        without = diag.gather(tmp_path, "https://dash.example")
        with_cellular = diag.gather(tmp_path, "https://dash.example", cellular_dir=cellular_dir)
        assert without.cellular is None
        assert with_cellular.cellular is not None
        assert with_cellular.cellular.state == cache.STATE_BUDGET  # type: ignore[union-attr]
        labels = [line.label for line in diag.diagnostic_lines(with_cellular, _NOW)]
        assert "Cellular" in labels and "Data" not in labels


# -- the pygame layer is wired to it ---------------------------------------------------------

pygame = pytest.importorskip("pygame")

from usage_dashboard.client.gui import DashboardGui  # noqa: E402


class _Fetcher:
    """Stands in for the GUI's own fetcher: holds readings and their read time,
    and counts refresh POSTs."""

    def __init__(self, readings: list[Reading], last_success_at: datetime | None) -> None:
        self._readings = readings
        self.last_success_at = last_success_at
        self.interval = 300
        self.refreshes = 0
        self.release = threading.Event()

    def get_latest_readings(self) -> list[Reading]:
        return self._readings

    @property
    def current_interval(self) -> int:
        return self.interval

    def request_refresh(self) -> bool:
        self.refreshes += 1
        self.release.wait(timeout=5)
        return True


@pytest.fixture
def surface():
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_mode((1280, 720))
    yield
    pygame.display.quit()


def _ago(minutes: float) -> datetime:
    """A timestamp relative to the real clock: the panel computes ages from it."""
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=minutes)


def _build(
    tmp_path: Path, cellular: bool, **gui_kwargs: Any
) -> tuple[DashboardGui, _Fetcher, Path]:
    state = tmp_path / "cellular"
    state.mkdir(exist_ok=True)
    fetcher = _Fetcher([_reading()], _ago(10))
    gui = DashboardGui(
        fetcher, (1280, 720),  # type: ignore[arg-type]
        cache_view=CacheView(state) if cellular else None,
        **gui_kwargs,
    )
    return gui, fetcher, state


def _frame(gui: DashboardGui) -> layout.MainLayout:
    """One frame's worth of the main loop's layout, without running the loop."""
    readings, age, source_label, interval = gui._displayed()
    return layout.build_main_layout(
        readings, (1280, 720), refresh_interval=interval, age_seconds=age,
        source_label=source_label,
    )


def _publish_cellular(state: Path, minutes_ago: float, percent: float = 77.0) -> datetime:
    written_at = _ago(minutes_ago)
    cache.publish(
        cache.CacheEntry(
            written_at, cache.SOURCE_CELLULAR, 600, _payload(_reading(percent=percent))
        ),
        state,
    )
    return written_at


def _await_request(state: Path) -> float:
    """The token of the re-read the refresh thread is about to post. The thread
    asks for it a moment after the tap, so wait for it to show up."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        token = cache.read_refresh_token(state / cache.REQUEST_DIR)
        if token is not None:
            return token
        time.sleep(0.01)
    raise AssertionError("the refresh thread never requested a re-read")


class TestPanelWiring:
    def test_without_a_cache_view_nothing_changes(self, surface: None, tmp_path: Path) -> None:
        gui, _fetcher, state = _build(tmp_path, cellular=False)
        text = _frame(gui).status_text
        assert SOURCE_LABEL not in text and text.endswith(" · refresh 5m · 1 provider")
        gui._report_wake_state(False)
        assert not (state / cache.REQUEST_DIR).exists()

    def test_a_newer_cellular_read_is_drawn_and_named(self, surface: None, tmp_path: Path) -> None:
        gui, _fetcher, state = _build(tmp_path, cellular=True)
        written_at = _publish_cellular(state, minutes_ago=2)
        frame = _frame(gui)
        assert frame.tiles[0].bars[0].fraction == 0.77
        # Two minutes old on a ten-minute cadence: no age worth stating yet.
        assert " · cellular · refresh 10m · 1 provider" in frame.status_text
        assert gui._on_cellular is True
        # The cache stores whole seconds.
        assert gui._shown_success_at == written_at.replace(microsecond=0)

    def test_its_own_newer_read_still_wins(self, surface: None, tmp_path: Path) -> None:
        gui, _fetcher, _state = _build(tmp_path, cellular=True)
        _publish_cellular(state=_state, minutes_ago=19)
        frame = _frame(gui)
        assert frame.tiles[0].bars[0].fraction == 0.40
        assert SOURCE_LABEL not in frame.status_text and gui._on_cellular is False

    def test_a_preserved_payload_shows_its_age_after_a_restart(
        self, surface: None, tmp_path: Path
    ) -> None:
        # No successful read of its own since starting, and a three-hour-old
        # cellular payload: the panel says exactly that.
        gui, fetcher, state = _build(tmp_path, cellular=True)
        fetcher.last_success_at = None
        fetcher._readings = []
        _publish_cellular(state, minutes_ago=180)
        status_text = _frame(gui).status_text
        assert re.search(r" · cellular · 3h \d+m ago · refresh 10m · 1 provider$", status_text)

    def test_the_worker_is_told_when_the_panel_is_lit_and_when_it_is_dark(
        self, surface: None, tmp_path: Path
    ) -> None:
        gui, _fetcher, state = _build(tmp_path, cellular=True)
        awake = state / cache.REQUEST_DIR / cache.AWAKE_FILE
        gui._report_wake_state(False)      # lit
        assert awake.exists()
        gui._report_wake_state(True)       # dark
        assert not awake.exists()

    def test_refresh_on_cellular_rereads_instead_of_posting(
        self, surface: None, tmp_path: Path
    ) -> None:
        gui, fetcher, state = _build(tmp_path, cellular=True)
        _publish_cellular(state, minutes_ago=2)
        _frame(gui)                        # the panel now shows the cellular payload
        gui._start_refresh()
        assert fetcher.refreshes == 0      # nothing asked the server for a collection
        assert gui._refresh_feedback == "refreshing\u2026"
        thread = gui._refresh_thread
        assert thread is not None
        # The worker's re-read lands while the tap is waiting.
        _publish_cellular(state, minutes_ago=0, percent=81.0)
        thread.join(timeout=10)
        assert gui._refresh_feedback == "refreshed" and gui._refresh_pending is False
        assert cache.read_refresh_token(state / cache.REQUEST_DIR) is not None

    def test_refresh_on_cellular_reports_a_declined_tap(
        self, surface: None, tmp_path: Path
    ) -> None:
        gui, _fetcher, state = _build(tmp_path, cellular=True)
        _publish_cellular(state, minutes_ago=2)
        _frame(gui)
        gui._start_refresh()
        token = _await_request(state)
        cache.write_status(_status(request=cache.RequestOutcome(token, "deferred")), state)
        thread = gui._refresh_thread
        assert thread is not None
        thread.join(timeout=10)
        assert gui._refresh_feedback == "refresh deferred"

    def test_refresh_on_cellular_reports_no_answer(
        self, surface: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The worker never answers; only the wait is shortened.
        monkeypatch.setattr(cache_view, "REREAD_WAIT_SECONDS", 0.5)
        gui, _fetcher, state = _build(tmp_path, cellular=True)
        _publish_cellular(state, minutes_ago=2)
        _frame(gui)
        gui._start_refresh()
        thread = gui._refresh_thread
        assert thread is not None
        thread.join(timeout=20)
        assert gui._refresh_feedback == "refresh failed" and gui._refresh_pending is False

    def test_refresh_on_the_normal_route_still_posts(self, surface: None, tmp_path: Path) -> None:
        gui, fetcher, _state = _build(tmp_path, cellular=True)
        _frame(gui)
        assert gui._on_cellular is False
        gui._start_refresh()
        assert fetcher.refreshes == 1
        fetcher.release.set()
        thread = gui._refresh_thread
        assert thread is not None
        thread.join(timeout=5)
        assert gui._refresh_feedback == "refreshed"


# -- two points an independent review raised ---------------------------------------------------


class TestFutureDatedCache:
    def _own(self) -> list[Reading]:
        return [_reading(percent=40.0)]

    def test_an_entry_stamped_ahead_of_the_clock_is_not_preferred(
        self, tmp_path: Path
    ) -> None:
        # The clock stepped backwards after the worker wrote this. Taken at
        # its word it would outrank every newer reading for as long as the
        # step, and show an age of zero the whole time.
        clock = [_ago(0).timestamp()]
        view = CacheView(tmp_path, now=lambda: clock[0])
        cache.publish(
            cache.CacheEntry(
                _ago(-2 * 86400), cache.SOURCE_CELLULAR, 600, _payload(_reading(percent=77.0))
            ),
            tmp_path,
        )
        selection = view.select(self._own(), _ago(600))
        assert selection.readings == self._own()
        assert selection.cellular is False
        # Small clock noise (30 s; _ago takes minutes) is not a step, and is
        # still preferred.
        cache.publish(
            cache.CacheEntry(
                _ago(-0.5), cache.SOURCE_CELLULAR, 600, _payload(_reading(percent=77.0))
            ),
            tmp_path,
        )
        assert view.select(self._own(), _ago(600)).cellular is True

    def test_an_entry_from_a_clock_that_has_passed_is_normal(self, tmp_path: Path) -> None:
        clock = [_ago(0).timestamp()]
        view = CacheView(tmp_path, now=lambda: clock[0])
        cache.publish(
            cache.CacheEntry(_ago(60), cache.SOURCE_CELLULAR, 600, _payload(_reading())),
            tmp_path,
        )
        assert view.select([], None).cellular is True


class TestWorkerNotRunning:
    def _status(self, tmp_path: Path, **overrides: Any) -> cache.WorkerStatus:
        cellular_dir = tmp_path / "cellular"
        cellular_dir.mkdir(exist_ok=True)
        status = _status(**overrides)
        cache.write_status(status, cellular_dir)
        return status

    def test_a_worker_that_stopped_cleanly_is_said_to_have_stopped(
        self, tmp_path: Path
    ) -> None:
        # It withdraws its next poll time on the way out; "stalled" would be
        # wrong, and silence would be worse.
        status = self._status(tmp_path, state=cache.STATE_FALLBACK, next_poll_at=None)
        lines = diag.cellular_lines(status, _NOW)
        assert lines[0] == diag.DiagLine("Cellular", "worker stopped", warn=True)

    def test_a_running_worker_in_fallback_is_not_called_stopped(self, tmp_path: Path) -> None:
        status = self._status(tmp_path, state=cache.STATE_FALLBACK)
        assert "stopped" not in diag.cellular_lines(status, _NOW)[0].value
