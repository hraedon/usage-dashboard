from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

from usage_dashboard.server.db import Database
from usage_dashboard.server.fetch_types import FetchError
from usage_dashboard.server.scheduler import FetchScheduler
from usage_dashboard.shared.models import (
    Provider,
    Reading,
    ReadingStatus,
)


def _make_reading(**overrides: object) -> Reading:
    defaults = {
        "provider": Provider.CLAUDE,
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


class TestFetchSchedulerSuccessful:
    def test_successful_fetch_stores_reading(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        reading = _make_reading(provider=Provider.CLAUDE)
        fetch_fn = MagicMock(return_value=reading)
        scheduler = FetchScheduler(db, claude_token="token")
        scheduler._fetch_one(Provider.CLAUDE, fetch_fn)
        result = db.get_latest_readings()
        assert Provider.CLAUDE in result
        assert result[Provider.CLAUDE].session_percent == 50.0

    def test_successful_fetch_resets_failures(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        db.increment_failures(Provider.CLAUDE)
        db.increment_failures(Provider.CLAUDE)
        reading = _make_reading(provider=Provider.CLAUDE)
        fetch_fn = MagicMock(return_value=reading)
        scheduler = FetchScheduler(db, claude_token="token")
        scheduler._fetch_one(Provider.CLAUDE, fetch_fn)
        assert db.get_consecutive_failures(Provider.CLAUDE) == 0

    def test_successful_fetch_stores_zero_failures(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        db.increment_failures(Provider.CLAUDE)
        reading = _make_reading(provider=Provider.CLAUDE)
        fetch_fn = MagicMock(return_value=reading)
        scheduler = FetchScheduler(db, claude_token="token")
        scheduler._fetch_one(Provider.CLAUDE, fetch_fn)
        assert db.get_consecutive_failures(Provider.CLAUDE) == 0


class TestFetchSchedulerFailed:
    def test_failed_fetch_increments_failures(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        fetch_fn = MagicMock(side_effect=FetchError("fail"))
        scheduler = FetchScheduler(db, claude_token="token")
        scheduler._fetch_one(Provider.CLAUDE, fetch_fn)
        assert db.get_consecutive_failures(Provider.CLAUDE) == 1

    def test_failed_fetch_stores_stale_reading_when_existing(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        original = _make_reading(
            provider=Provider.CLAUDE, stale=False, status=ReadingStatus.CURRENT,
        )
        db.store_reading(original)
        fetch_fn = MagicMock(side_effect=FetchError("fail"))
        scheduler = FetchScheduler(db, claude_token="token")
        scheduler._fetch_one(Provider.CLAUDE, fetch_fn)
        result = db.get_latest_readings()
        assert result[Provider.CLAUDE].stale is True
        assert result[Provider.CLAUDE].status is ReadingStatus.STALE

    def test_failed_fetch_stores_stale_when_no_prior_reading(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        fetch_fn = MagicMock(side_effect=FetchError("fail"))
        scheduler = FetchScheduler(db, claude_token="token")
        scheduler._fetch_one(Provider.CLAUDE, fetch_fn)
        result = db.get_latest_readings()
        assert result[Provider.CLAUDE].stale is True

    def test_24_consecutive_failures_marks_offline(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        original = _make_reading(provider=Provider.CLAUDE)
        db.store_reading(original)
        fetch_fn = MagicMock(side_effect=FetchError("fail"))
        scheduler = FetchScheduler(db, claude_token="token", offline_threshold=24)
        for _ in range(24):
            scheduler._fetch_one(Provider.CLAUDE, fetch_fn)
        result = db.get_latest_readings()
        assert result[Provider.CLAUDE].status is ReadingStatus.OFFLINE
        assert db.get_consecutive_failures(Provider.CLAUDE) == 24

    def test_23_failures_stays_stale(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        original = _make_reading(provider=Provider.CLAUDE)
        db.store_reading(original)
        fetch_fn = MagicMock(side_effect=FetchError("fail"))
        scheduler = FetchScheduler(db, claude_token="token", offline_threshold=24)
        for _ in range(23):
            scheduler._fetch_one(Provider.CLAUDE, fetch_fn)
        result = db.get_latest_readings()
        assert result[Provider.CLAUDE].status is ReadingStatus.STALE

    def test_custom_offline_threshold(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        original = _make_reading(provider=Provider.CLAUDE)
        db.store_reading(original)
        fetch_fn = MagicMock(side_effect=FetchError("fail"))
        scheduler = FetchScheduler(db, claude_token="token", offline_threshold=3)
        for _ in range(3):
            scheduler._fetch_one(Provider.CLAUDE, fetch_fn)
        result = db.get_latest_readings()
        assert result[Provider.CLAUDE].status is ReadingStatus.OFFLINE


class TestFetchSchedulerNoProviders:
    def test_no_configured_providers_empty_tasks(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(db)
        tasks = scheduler._get_fetch_tasks()
        assert tasks == []

    def test_only_claude_configured(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(db, claude_token="token")
        tasks = scheduler._get_fetch_tasks()
        assert len(tasks) == 1
        assert tasks[0][0] is Provider.CLAUDE

    def test_only_zai_configured(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(db, zai_key="key")
        tasks = scheduler._get_fetch_tasks()
        assert len(tasks) == 1
        assert tasks[0][0] is Provider.ZAI

    def test_ollama_requires_both_credentials(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(db, ollama_email="e@e.com")
        tasks = scheduler._get_fetch_tasks()
        assert not any(p is Provider.OLLAMA for p, _ in tasks)

    def test_ollama_with_both_credentials(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(db, ollama_email="e@e.com", ollama_password="pw")
        tasks = scheduler._get_fetch_tasks()
        assert any(p is Provider.OLLAMA for p, _ in tasks)
