from __future__ import annotations

import time
from datetime import datetime
from unittest.mock import MagicMock, patch

import httpx

from usage_dashboard.client.fetcher import ClientFetcher
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus


def _make_reading(provider: Provider = Provider.CLAUDE, **overrides: object) -> Reading:
    defaults = {
        "provider": provider,
        "status": ReadingStatus.CURRENT,
        "session_percent": 50.0,
        "session_resets_at": datetime(2026, 1, 15, 10, 0, 0),
        "weekly_percent": 60.0,
        "weekly_resets_at": datetime(2026, 1, 19, 0, 0, 0),
        "fetched_at": datetime(2026, 1, 14, 12, 0, 0),
        "stale": False,
    }
    defaults.update(overrides)
    return Reading(**defaults)  # type: ignore[arg-type]


class TestReadingsChanged:
    def test_identical_readings_returns_false(self):
        old = [_make_reading()]
        new = [_make_reading()]
        assert ClientFetcher._readings_changed(old, new) is False

    def test_different_session_percent_returns_true(self):
        old = [_make_reading(session_percent=50.0)]
        new = [_make_reading(session_percent=80.0)]
        assert ClientFetcher._readings_changed(old, new) is True

    def test_different_weekly_percent_returns_true(self):
        old = [_make_reading(weekly_percent=60.0)]
        new = [_make_reading(weekly_percent=90.0)]
        assert ClientFetcher._readings_changed(old, new) is True

    def test_different_length_returns_true(self):
        old = [_make_reading()]
        new = [_make_reading(), _make_reading(provider=Provider.ZAI)]
        assert ClientFetcher._readings_changed(old, new) is True

    def test_new_provider_returns_true(self):
        old = [_make_reading(provider=Provider.CLAUDE)]
        new = [_make_reading(provider=Provider.ZAI)]
        assert ClientFetcher._readings_changed(old, new) is True

    def test_none_to_value_returns_true(self):
        old = [_make_reading(session_percent=None)]
        new = [_make_reading(session_percent=50.0)]
        assert ClientFetcher._readings_changed(old, new) is True

    def test_value_to_none_returns_true(self):
        old = [_make_reading(session_percent=50.0)]
        new = [_make_reading(session_percent=None)]
        assert ClientFetcher._readings_changed(old, new) is True

    def test_both_none_returns_false(self):
        old = [_make_reading(session_percent=None)]
        new = [_make_reading(session_percent=None)]
        assert ClientFetcher._readings_changed(old, new) is False

    def test_empty_lists_returns_false(self):
        assert ClientFetcher._readings_changed([], []) is False

    def test_multiple_providers_one_changed(self):
        old = [
            _make_reading(provider=Provider.CLAUDE, session_percent=50.0),
            _make_reading(provider=Provider.ZAI, session_percent=40.0),
        ]
        new = [
            _make_reading(provider=Provider.CLAUDE, session_percent=50.0),
            _make_reading(provider=Provider.ZAI, session_percent=70.0),
        ]
        assert ClientFetcher._readings_changed(old, new) is True


class TestAdaptiveInterval:
    def test_change_switches_to_fast_interval(self):
        fetcher = ClientFetcher("http://localhost", "key", default_interval=300, fast_interval=60)
        fetcher._readings = [_make_reading(session_percent=50.0)]
        new = [_make_reading(session_percent=80.0)]
        fetcher._update_interval(new)
        assert fetcher._interval == 60

    def test_stable_reaches_default_interval(self):
        fetcher = ClientFetcher(
            "http://localhost", "key",
            default_interval=300, fast_interval=60, stable_threshold=3,
        )
        fetcher._readings = [_make_reading(session_percent=50.0)]
        changed = [_make_reading(session_percent=80.0)]
        fetcher._update_interval(changed)
        assert fetcher._interval == 60
        fetcher._readings = changed
        same = [_make_reading(session_percent=80.0)]
        fetcher._update_interval(same)
        fetcher._readings = same
        fetcher._update_interval(same)
        fetcher._readings = same
        fetcher._update_interval(same)
        assert fetcher._interval == 300

    def test_stable_count_resets_on_change(self):
        fetcher = ClientFetcher(
            "http://localhost", "key",
            default_interval=300, fast_interval=60, stable_threshold=3,
        )
        fetcher._readings = [_make_reading(session_percent=50.0)]
        same = [_make_reading(session_percent=50.0)]
        fetcher._update_interval(same)
        assert fetcher._stable_count == 1
        changed = [_make_reading(session_percent=80.0)]
        fetcher._update_interval(changed)
        assert fetcher._stable_count == 0
        assert fetcher._interval == 60

    def test_initial_readings_empty_first_fetch_is_change(self):
        fetcher = ClientFetcher("http://localhost", "key", default_interval=300, fast_interval=60)
        fetcher._readings = []
        new = [_make_reading()]
        fetcher._update_interval(new)
        assert fetcher._interval == 60


class TestSchedulePolling:
    def _resp(self, payload):
        r = MagicMock()
        r.raise_for_status = MagicMock()
        r.json.return_value = payload
        return r

    @patch("usage_dashboard.client.fetcher.httpx.get")
    def test_poll_schedule_sets_spec_and_sends_unit(self, mock_get):
        mock_get.return_value = self._resp({"schedule": "daily 00:00-08:00"})
        f = ClientFetcher("http://srv", "key", unit_id="mpmusage02", fetch_schedule=True)
        f._poll_schedule()
        assert f.current_schedule_spec == "daily 00:00-08:00"
        # unit id passed through as the ?unit= query param
        assert mock_get.call_args.kwargs["params"] == {"unit": "mpmusage02"}

    @patch("usage_dashboard.client.fetcher.httpx.get")
    def test_poll_schedule_null_when_server_has_none(self, mock_get):
        mock_get.return_value = self._resp({"schedule": None})
        f = ClientFetcher("http://srv", "key", fetch_schedule=True)
        f._poll_schedule()
        assert f.current_schedule_spec is None

    @patch("usage_dashboard.client.fetcher.httpx.get")
    def test_poll_schedule_error_keeps_previous(self, mock_get):
        f = ClientFetcher("http://srv", "key", unit_id="u1", fetch_schedule=True)
        f._schedule_spec = "daily 00:00-08:00"  # previously good
        mock_get.side_effect = httpx.ConnectError("down")
        f._poll_schedule()
        assert f.current_schedule_spec == "daily 00:00-08:00"  # not clobbered


class TestRequestRefresh:
    """On-demand refresh (WI-012): request_refresh() POSTs /refresh and pokes
    the poll loop so fresh readings are fetched immediately."""

    def _resp(self, status_code=200):
        r = MagicMock()
        r.status_code = status_code
        r.raise_for_status = MagicMock()
        return r

    @patch("usage_dashboard.client.fetcher.httpx.post")
    def test_posts_to_refresh_with_auth_and_pokes(self, mock_post):
        mock_post.return_value = self._resp(200)
        f = ClientFetcher("http://srv", "key")
        assert f.request_refresh() is True
        assert mock_post.call_args.args[0] == "http://srv/refresh"
        assert mock_post.call_args.kwargs["headers"] == {
            "Authorization": "Bearer key"
        }
        # The poke wakes the poll wait so the loop fetches right away.
        assert f._wake_event.is_set()

    @patch("usage_dashboard.client.fetcher.httpx.post")
    def test_rate_limited_returns_false_without_poke(self, mock_post):
        mock_post.return_value = self._resp(429)
        f = ClientFetcher("http://srv", "key")
        assert f.request_refresh() is False
        assert not f._wake_event.is_set()

    @patch("usage_dashboard.client.fetcher.httpx.post")
    def test_network_error_returns_false(self, mock_post):
        mock_post.side_effect = httpx.ConnectError("down")
        f = ClientFetcher("http://srv", "key")
        assert f.request_refresh() is False
        assert not f._wake_event.is_set()

    @patch("usage_dashboard.client.fetcher.httpx.post")
    def test_other_status_returns_false(self, mock_post):
        resp = self._resp(500)
        request = MagicMock()
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=request, response=resp,
        )
        mock_post.return_value = resp
        f = ClientFetcher("http://srv", "key")
        assert f.request_refresh() is False


class TestPokeWake:
    def test_poke_wakes_poll_wait_early(self):
        f = ClientFetcher("http://srv", "key", default_interval=300)
        f._interval = 300
        f._wake_event.set()  # a poke already arrived
        start = time.monotonic()
        f._wait_for_next_poll()
        assert time.monotonic() - start < 2.0

    def test_wait_blocks_until_timeout_without_poke(self):
        f = ClientFetcher("http://srv", "key")
        f._interval = 0.2
        start = time.monotonic()
        f._wait_for_next_poll()
        assert time.monotonic() - start >= 0.15

    def test_stop_event_interrupts_wait(self):
        f = ClientFetcher("http://srv", "key")
        f._interval = 300
        f._stop_event.set()
        start = time.monotonic()
        f._wait_for_next_poll()
        assert time.monotonic() - start < 2.0
