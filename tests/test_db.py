from __future__ import annotations

from datetime import datetime

from usage_dashboard.server.db import Database
from usage_dashboard.shared.models import ModelUsage, Provider, Reading, ReadingStatus


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

    def test_latest_reading_returned_after_multiple_stores(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()
        first = _make_reading(session_percent=30.0)
        db.store_reading(first)
        second = _make_reading(session_percent=80.0)
        db.store_reading(second)
        result = db.get_latest_readings()
        assert len(result) == 1
        assert result[Provider.CLAUDE].session_percent == 80.0

    def test_append_only_keeps_history(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()
        db.store_reading(_make_reading(session_percent=30.0))
        db.store_reading(_make_reading(session_percent=60.0))
        db.store_reading(_make_reading(session_percent=90.0))
        with db._lock:
            count = db._conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        assert count == 3

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

    def test_store_reading_does_not_touch_failures(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()
        db.increment_failures(Provider.CLAUDE)
        db.increment_failures(Provider.CLAUDE)
        db.increment_failures(Provider.CLAUDE)
        reading = _make_reading()
        db.store_reading(reading)
        assert db.get_consecutive_failures(Provider.CLAUDE) == 3

    def test_reset_failures_after_store(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()
        db.increment_failures(Provider.CLAUDE)
        db.increment_failures(Provider.CLAUDE)
        db.increment_failures(Provider.CLAUDE)
        db.reset_failures(Provider.CLAUDE)
        assert db.get_consecutive_failures(Provider.CLAUDE) == 0

    def test_reading_with_none_percent_stored_and_retrieved(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()
        reading = _make_reading(session_percent=None, weekly_percent=None)
        db.store_reading(reading)
        result = db.get_latest_readings()
        assert result[Provider.CLAUDE].session_percent is None
        assert result[Provider.CLAUDE].weekly_percent is None


class TestDetailColumn:
    def test_detail_stored_and_retrieved(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()
        reading = _make_reading(provider=Provider.UMANS, detail="pk 2/4  req 161  tok 63.9M")
        db.store_reading(reading)
        result = db.get_latest_readings()[Provider.UMANS]
        assert result.detail == "pk 2/4  req 161  tok 63.9M"

    def test_initialize_migrates_legacy_schema_without_detail(self, tmp_path):
        import sqlite3

        db_path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE readings (
                provider TEXT NOT NULL,
                status TEXT NOT NULL,
                session_percent REAL,
                session_resets_at TEXT,
                weekly_percent REAL,
                weekly_resets_at TEXT,
                fetched_at TEXT NOT NULL,
                stale INTEGER NOT NULL DEFAULT 0,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (provider)
            )
            """
        )
        conn.commit()
        conn.close()

        db = Database(db_path)
        db.initialize()
        reading = _make_reading(detail="some detail")
        db.store_reading(reading)
        result = db.get_latest_readings()[reading.provider]
        assert result.detail == "some detail"


class TestModelsColumn:
    def test_models_stored_and_retrieved(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()
        reading = _make_reading(
            provider=Provider.OLLAMA,
            models=[
                ModelUsage(name="minimax-m3", requests=100, share_percent=80.0),
                ModelUsage(name="glm-5.2", requests=20, share_percent=20.0),
            ],
        )
        db.store_reading(reading)
        result = db.get_latest_readings()[Provider.OLLAMA]
        assert result.models is not None
        assert len(result.models) == 2
        assert result.models[0].name == "minimax-m3"
        assert result.models[0].requests == 100
        assert result.models[1].name == "glm-5.2"

    def test_none_models_stored_and_retrieved(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()
        reading = _make_reading(provider=Provider.CLAUDE)
        db.store_reading(reading)
        result = db.get_latest_readings()[Provider.CLAUDE]
        assert result.models is None

    def test_initialize_migrates_legacy_schema_without_models(self, tmp_path):
        import sqlite3

        db_path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE readings (
                provider TEXT NOT NULL,
                status TEXT NOT NULL,
                session_percent REAL,
                session_resets_at TEXT,
                weekly_percent REAL,
                weekly_resets_at TEXT,
                fetched_at TEXT NOT NULL,
                stale INTEGER NOT NULL DEFAULT 0,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                detail TEXT,
                PRIMARY KEY (provider)
            )
            """
        )
        conn.commit()
        conn.close()

        db = Database(db_path)
        db.initialize()
        reading = _make_reading(
            models=[ModelUsage(name="test-model", requests=5, share_percent=50.0)]
        )
        db.store_reading(reading)
        result = db.get_latest_readings()[reading.provider]
        assert result.models is not None
        assert result.models[0].name == "test-model"


class TestPruneOldReadings:
    def test_prune_old_readings_deletes_old(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()
        old = _make_reading(fetched_at=datetime(2026, 1, 1, 12, 0, 0))
        recent = _make_reading(fetched_at=datetime(2026, 7, 4, 12, 0, 0))
        db.store_reading(old)
        db.store_reading(recent)
        deleted = db.prune_old_readings(7)
        assert deleted == 1
        result = db.get_latest_readings()
        assert len(result) == 1
        assert result[Provider.CLAUDE].fetched_at == recent.fetched_at

    def test_prune_old_readings_keeps_all_within_retention(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()
        r1 = _make_reading(fetched_at=datetime(2026, 7, 3, 12, 0, 0))
        r2 = _make_reading(fetched_at=datetime(2026, 7, 4, 12, 0, 0))
        db.store_reading(r1)
        db.store_reading(r2)
        deleted = db.prune_old_readings(7)
        assert deleted == 0
        result = db.get_latest_readings()
        assert len(result) == 1
        assert result[Provider.CLAUDE].fetched_at == r2.fetched_at


class TestSchemaMigration:
    def test_initialize_migrates_old_schema(self, tmp_path):
        import sqlite3

        db_path = str(tmp_path / "old.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE readings (
                provider TEXT NOT NULL,
                status TEXT NOT NULL,
                session_percent REAL,
                session_resets_at TEXT,
                weekly_percent REAL,
                weekly_resets_at TEXT,
                fetched_at TEXT NOT NULL,
                stale INTEGER NOT NULL DEFAULT 0,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                detail TEXT,
                models TEXT,
                throttle TEXT NOT NULL DEFAULT 'none',
                PRIMARY KEY (provider)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO readings (provider, status, session_percent, fetched_at,
                                   stale, consecutive_failures, detail)
            VALUES ('claude', 'current', 42.0, '2026-01-14T12:00:00', 0, 3, 'hello')
            """
        )
        conn.commit()
        conn.close()

        db = Database(db_path)
        db.initialize()
        assert db.get_consecutive_failures(Provider.CLAUDE) == 3
        result = db.get_latest_readings()
        assert result[Provider.CLAUDE].session_percent == 42.0
        assert result[Provider.CLAUDE].detail == "hello"
        with db._lock:
            count = db._conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        assert count == 1

    def test_initialize_idempotent_on_new_schema(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        db = Database(db_path)
        db.initialize()
        db.initialize()
        reading = _make_reading()
        db.store_reading(reading)
        result = db.get_latest_readings()
        assert result[Provider.CLAUDE] == reading
