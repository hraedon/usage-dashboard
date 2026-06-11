from __future__ import annotations

from datetime import datetime

from usage_dashboard.server.db import Database
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus


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


class TestDatabase:
    def test_store_and_retrieve_reading(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()
        reading = _make_reading()
        db.store_reading(reading)
        result = db.get_latest_readings()
        assert Provider.CLAUDE in result
        assert result[Provider.CLAUDE] == reading

    def test_upsert_replaces_existing_reading(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()
        first = _make_reading(session_percent=30.0)
        db.store_reading(first)
        second = _make_reading(session_percent=80.0)
        db.store_reading(second)
        result = db.get_latest_readings()
        assert len(result) == 1
        assert result[Provider.CLAUDE].session_percent == 80.0

    def test_store_multiple_providers(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()
        claude = _make_reading(provider=Provider.CLAUDE)
        zai = _make_reading(provider=Provider.ZAI)
        db.store_reading(claude)
        db.store_reading(zai)
        result = db.get_latest_readings()
        assert len(result) == 2
        assert result[Provider.CLAUDE] == claude
        assert result[Provider.ZAI] == zai

    def test_empty_database_returns_empty_dict(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()
        result = db.get_latest_readings()
        assert result == {}

    def test_consecutive_failures_starts_at_zero(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()
        assert db.get_consecutive_failures(Provider.CLAUDE) == 0

    def test_increment_failures(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()
        count = db.increment_failures(Provider.CLAUDE)
        assert count == 1
        count = db.increment_failures(Provider.CLAUDE)
        assert count == 2
        count = db.increment_failures(Provider.CLAUDE)
        assert count == 3

    def test_get_consecutive_failures_after_increment(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()
        db.increment_failures(Provider.CLAUDE)
        db.increment_failures(Provider.CLAUDE)
        assert db.get_consecutive_failures(Provider.CLAUDE) == 2

    def test_reset_failures(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()
        db.increment_failures(Provider.CLAUDE)
        db.increment_failures(Provider.CLAUDE)
        db.reset_failures(Provider.CLAUDE)
        assert db.get_consecutive_failures(Provider.CLAUDE) == 0

    def test_reset_failures_idempotent(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()
        db.reset_failures(Provider.CLAUDE)
        assert db.get_consecutive_failures(Provider.CLAUDE) == 0

    def test_failures_tracked_per_provider(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()
        db.increment_failures(Provider.CLAUDE)
        db.increment_failures(Provider.CLAUDE)
        db.increment_failures(Provider.ZAI)
        assert db.get_consecutive_failures(Provider.CLAUDE) == 2
        assert db.get_consecutive_failures(Provider.ZAI) == 1

    def test_store_reading_with_consecutive_failures(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()
        reading = _make_reading()
        db.store_reading(reading, consecutive_failures=5)
        assert db.get_consecutive_failures(Provider.CLAUDE) == 5

    def test_upsert_preserves_latest_failure_count(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()
        reading = _make_reading()
        db.store_reading(reading, consecutive_failures=3)
        new_reading = _make_reading(session_percent=90.0)
        db.store_reading(new_reading, consecutive_failures=0)
        assert db.get_consecutive_failures(Provider.CLAUDE) == 0

    def test_reading_with_none_percent_stored_and_retrieved(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()
        reading = _make_reading(session_percent=None, weekly_percent=None)
        db.store_reading(reading)
        result = db.get_latest_readings()
        assert result[Provider.CLAUDE].session_percent is None
        assert result[Provider.CLAUDE].weekly_percent is None
