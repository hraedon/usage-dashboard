from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from usage_dashboard.server.db import Database
from usage_dashboard.server.fetch_types import FetchError
from usage_dashboard.server.scheduler import FetchScheduler
from usage_dashboard.server.token_store import TokenStore
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

    def test_unexpected_exception_contained_and_recorded(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        db.store_reading(_make_reading(provider=Provider.UMANS))
        fetch_fn = MagicMock(
            side_effect=AttributeError("'NoneType' has no attribute 'get'")
        )
        scheduler = FetchScheduler(db)
        # A non-FetchError from one fetcher must not escape: if it does,
        # _run_loop's thread dies and every provider stops fetching.
        scheduler._fetch_one(Provider.UMANS, fetch_fn)
        assert db.get_consecutive_failures(Provider.UMANS) == 1
        assert db.get_latest_readings()[Provider.UMANS].stale is True


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

    def test_ollama_not_registered_without_cookie(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(db)
        tasks = scheduler._get_fetch_tasks()
        assert not any(p is Provider.OLLAMA for p, _ in tasks)

    def test_ollama_registered_with_cookie(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(db, ollama_cookie="session=abc")
        tasks = scheduler._get_fetch_tasks()
        assert any(p is Provider.OLLAMA for p, _ in tasks)


class TestClaudeRefreshGating:
    def test_no_refresh_attempt_on_transient_failure(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(
            db, claude_token="token", claude_refresh_token="refresh"
        )
        scheduler._try_refresh_claude = MagicMock(return_value=True)  # type: ignore[method-assign]
        fetch_fn = MagicMock(side_effect=FetchError("HTTP 429"))
        scheduler._fetch_one(Provider.CLAUDE, fetch_fn)
        scheduler._try_refresh_claude.assert_not_called()

    def test_refresh_attempted_on_auth_failure(self, tmp_path):
        from unittest.mock import patch

        from usage_dashboard.server.fetch_types import FetchAuthError

        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(
            db, claude_token="token", claude_refresh_token="refresh"
        )
        scheduler._try_refresh_claude = MagicMock(return_value=True)  # type: ignore[method-assign]
        fetch_fn = MagicMock(side_effect=FetchAuthError("HTTP 401"))
        retry_reading = _make_reading(provider=Provider.CLAUDE)
        with patch(
            "usage_dashboard.server.scheduler.fetch_claude_usage",
            return_value=retry_reading,
        ):
            scheduler._fetch_one(Provider.CLAUDE, fetch_fn)
        scheduler._try_refresh_claude.assert_called_once()
        assert db.get_latest_readings()[Provider.CLAUDE].status is ReadingStatus.CURRENT


class TestRateLimitBackoff:
    def test_rate_limit_blocks_provider_until_retry_after(self, tmp_path):
        from usage_dashboard.server.fetch_types import FetchRateLimitError

        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(db, claude_token="token")
        fetch_fn = MagicMock(
            side_effect=FetchRateLimitError("HTTP 429", retry_after_seconds=1691.0)
        )
        scheduler._fetch_one(Provider.CLAUDE, fetch_fn)

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        assert scheduler._is_blocked(Provider.CLAUDE, now)
        assert not scheduler._is_blocked(Provider.CLAUDE, now + timedelta(seconds=1700))
        # expiry check above also clears the block
        assert not scheduler._is_blocked(Provider.CLAUDE, now)

    def test_rate_limit_without_retry_after_uses_default(self, tmp_path):
        from usage_dashboard.server.fetch_types import FetchRateLimitError

        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(db, claude_token="token", rate_limit_default_seconds=120)
        fetch_fn = MagicMock(side_effect=FetchRateLimitError("HTTP 429"))
        scheduler._fetch_one(Provider.CLAUDE, fetch_fn)

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        assert scheduler._is_blocked(Provider.CLAUDE, now)
        assert not scheduler._is_blocked(Provider.CLAUDE, now + timedelta(seconds=150))

    def test_rate_limit_still_marks_reading_stale(self, tmp_path):
        from usage_dashboard.server.fetch_types import FetchRateLimitError

        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        db.store_reading(_make_reading(provider=Provider.CLAUDE))
        scheduler = FetchScheduler(db, claude_token="token")
        fetch_fn = MagicMock(side_effect=FetchRateLimitError("HTTP 429"))
        scheduler._fetch_one(Provider.CLAUDE, fetch_fn)
        assert db.get_latest_readings()[Provider.CLAUDE].stale is True

    def test_other_providers_unaffected_by_block(self, tmp_path):
        from usage_dashboard.server.fetch_types import FetchRateLimitError

        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(db, claude_token="token", zai_key="key")
        fetch_fn = MagicMock(side_effect=FetchRateLimitError("HTTP 429"))
        scheduler._fetch_one(Provider.CLAUDE, fetch_fn)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        assert not scheduler._is_blocked(Provider.ZAI, now)


class TestClaudeTokenPersistence:
    def test_refresh_persists_to_token_store(self, tmp_path):
        from unittest.mock import patch

        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        token_store = TokenStore(str(tmp_path / "tokens.json"))
        scheduler = FetchScheduler(
            db,
            claude_token="old-access",
            claude_refresh_token="old-refresh",
            token_store=token_store,
        )
        with patch(
            "usage_dashboard.server.scheduler.refresh_claude_token",
            return_value=("new-access", "new-refresh"),
        ):
            result = scheduler._try_refresh_claude()
        assert result is True
        assert token_store.load_claude_tokens() == ("new-access", "new-refresh")
        assert scheduler._claude_token == "new-access"
        assert scheduler._claude_refresh_token == "new-refresh"

    def test_refresh_without_store_only_updates_memory(self, tmp_path):
        from unittest.mock import patch

        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(
            db,
            claude_token="old-access",
            claude_refresh_token="old-refresh",
        )
        with patch(
            "usage_dashboard.server.scheduler.refresh_claude_token",
            return_value=("new-access", "new-refresh"),
        ):
            result = scheduler._try_refresh_claude()
        assert result is True
        assert scheduler._claude_token == "new-access"
        assert scheduler._claude_refresh_token == "new-refresh"

    def test_no_refresh_token_returns_false(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        token_store = TokenStore(str(tmp_path / "tokens.json"))
        scheduler = FetchScheduler(
            db, claude_token="access", token_store=token_store,
        )
        assert scheduler._try_refresh_claude() is False

    def test_refresh_failure_does_not_persist(self, tmp_path):
        from unittest.mock import patch

        from usage_dashboard.server.fetch_types import FetchAuthError

        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        token_store = TokenStore(str(tmp_path / "tokens.json"))
        token_store.save_claude_tokens("saved-access", "saved-refresh")
        scheduler = FetchScheduler(
            db,
            claude_token="old-access",
            claude_refresh_token="old-refresh",
            token_store=token_store,
        )
        scheduler._try_refresh_claude = MagicMock(return_value=False)  # type: ignore[method-assign]
        fetch_fn = MagicMock(side_effect=FetchAuthError("HTTP 401"))
        with patch(
            "usage_dashboard.server.scheduler.fetch_claude_usage",
            side_effect=FetchError("still broken"),
        ):
            scheduler._fetch_one(Provider.CLAUDE, fetch_fn)
        # Store should still have the original saved tokens.
        assert token_store.load_claude_tokens() == ("saved-access", "saved-refresh")
