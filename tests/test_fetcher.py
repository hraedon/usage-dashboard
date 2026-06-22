from __future__ import annotations

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
