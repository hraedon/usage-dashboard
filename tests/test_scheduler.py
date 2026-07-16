from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from usage_dashboard.server.db import Database
from usage_dashboard.server.fetch_types import FetchAuthError, FetchError
from usage_dashboard.server.scheduler import FetchScheduler
from usage_dashboard.server.token_store import TokenStore
from usage_dashboard.shared.models import (
    THROTTLE_BOXED,
    THROTTLE_LOW,
    THROTTLE_LOW_INTERACTIVITY,
    THROTTLE_NONE,
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


class TestClaudeWorkAccount:
    def test_work_account_is_a_separate_fetch_task(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(
            db, claude_token="personal", claude_work_token="work"
        )
        assert scheduler.configured_providers() == [
            Provider.CLAUDE, Provider.CLAUDE_WORK
        ]

    def test_no_work_account_when_unconfigured(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(db, claude_token="personal")
        assert Provider.CLAUDE_WORK not in scheduler.configured_providers()

    def test_work_fetch_stores_under_its_own_provider(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(db, claude_work_token="work")
        reading = _make_reading(provider=Provider.CLAUDE_WORK, session_percent=12.0)
        scheduler._fetch_one(Provider.CLAUDE_WORK, MagicMock(return_value=reading))
        stored = db.get_latest_readings()
        assert Provider.CLAUDE_WORK in stored
        assert stored[Provider.CLAUDE_WORK].session_percent == 12.0
        assert Provider.CLAUDE not in stored  # personal untouched

    def test_work_refresh_persists_under_claude_work_key(self, tmp_path):
        from unittest.mock import patch

        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        token_store = TokenStore(str(tmp_path / "tokens.json"))
        scheduler = FetchScheduler(
            db,
            claude_token="p-access",
            claude_refresh_token="p-refresh",
            claude_work_token="w-access",
            claude_work_refresh_token="w-refresh",
            token_store=token_store,
        )
        with patch(
            "usage_dashboard.server.scheduler.refresh_claude_token",
            return_value=("new-w-access", "new-w-refresh"),
        ):
            assert scheduler._try_refresh_claude_work() is True
        # Work tokens persisted to their own namespace; personal untouched.
        assert token_store.get("claude_work") == ("new-w-access", "new-w-refresh")
        assert token_store.get("claude") == (None, None)
        assert scheduler._claude_work_token == "new-w-access"
        assert scheduler._claude_token == "p-access"

    def test_auth_failure_refreshes_the_work_account(self, tmp_path):
        from unittest.mock import patch

        from usage_dashboard.server.fetch_types import FetchAuthError

        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(db, claude_work_token="work")
        scheduler._try_refresh_claude_work = MagicMock(return_value=True)  # type: ignore[method-assign]
        scheduler._try_refresh_claude = MagicMock(return_value=True)  # type: ignore[method-assign]
        fetch_fn = MagicMock(side_effect=FetchAuthError("HTTP 401"))
        retry_reading = _make_reading(provider=Provider.CLAUDE_WORK)
        with patch(
            "usage_dashboard.server.scheduler.fetch_claude_usage",
            return_value=retry_reading,
        ):
            scheduler._fetch_one(Provider.CLAUDE_WORK, fetch_fn)
        scheduler._try_refresh_claude_work.assert_called_once()
        scheduler._try_refresh_claude.assert_not_called()  # only the work account
        assert db.get_latest_readings()[Provider.CLAUDE_WORK].status is ReadingStatus.CURRENT


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
        # The server's Retry-After is authoritative — it sets the gap, not the
        # exponential base (and may exceed the failure cap).
        snap = scheduler.schedule_snapshot()[Provider.CLAUDE]
        assert 1680.0 <= snap["interval_seconds"] <= 1700.0

    def test_rate_limit_default_is_a_floor_when_above_exponential(self, tmp_path):
        from usage_dashboard.server.fetch_types import FetchRateLimitError

        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        # A 429 with no Retry-After backs off by max(exponential, default).
        # With the default above the exponential base it dominates.
        scheduler = FetchScheduler(
            db, claude_token="token", rate_limit_default_seconds=1200
        )
        fetch_fn = MagicMock(side_effect=FetchRateLimitError("HTTP 429"))
        scheduler._fetch_one(Provider.CLAUDE, fetch_fn)

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        assert scheduler._is_blocked(Provider.CLAUDE, now + timedelta(seconds=600))
        assert not scheduler._is_blocked(Provider.CLAUDE, now + timedelta(seconds=1300))

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


def _interval(scheduler: FetchScheduler, provider: Provider) -> float:
    return scheduler.schedule_snapshot()[provider]["interval_seconds"]


class TestIdleLadder:
    def test_unchanged_reading_steps_interval_out(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        reading = _make_reading(provider=Provider.CLAUDE)
        fetch_fn = MagicMock(return_value=reading)
        scheduler = FetchScheduler(db, claude_token="token", interval_seconds=300)

        scheduler._fetch_one(Provider.CLAUDE, fetch_fn)
        assert _interval(scheduler, Provider.CLAUDE) == 300  # floor on first read
        for expected in (600, 900, 1800, 1800):  # widens, then caps at 30m
            scheduler._fetch_one(Provider.CLAUDE, fetch_fn)
            assert _interval(scheduler, Provider.CLAUDE) == expected

    def test_changed_reading_resets_to_floor(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(db, claude_token="token", interval_seconds=300)
        steady = _make_reading(provider=Provider.CLAUDE, session_percent=50.0)
        scheduler._fetch_one(Provider.CLAUDE, MagicMock(return_value=steady))
        scheduler._fetch_one(Provider.CLAUDE, MagicMock(return_value=steady))
        assert _interval(scheduler, Provider.CLAUDE) == 600

        moved = _make_reading(provider=Provider.CLAUDE, session_percent=80.0)
        scheduler._fetch_one(Provider.CLAUDE, MagicMock(return_value=moved))
        assert _interval(scheduler, Provider.CLAUDE) == 300

    def test_subepsilon_jitter_does_not_reset(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(
            db, claude_token="token", interval_seconds=300, change_epsilon=0.5
        )
        base = _make_reading(provider=Provider.CLAUDE, session_percent=50.0)
        scheduler._fetch_one(Provider.CLAUDE, MagicMock(return_value=base))
        scheduler._fetch_one(Provider.CLAUDE, MagicMock(return_value=base))
        assert _interval(scheduler, Provider.CLAUDE) == 600

        jitter = _make_reading(provider=Provider.CLAUDE, session_percent=50.2)
        scheduler._fetch_one(Provider.CLAUDE, MagicMock(return_value=jitter))
        assert _interval(scheduler, Provider.CLAUDE) == 900  # treated as unchanged

    def test_detail_change_resets_quotaless_provider(self, tmp_path):
        # umans carries movement in `detail`, not percentages.
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(db, umans_key="key", interval_seconds=300)
        r1 = _make_reading(
            provider=Provider.UMANS, session_percent=None, weekly_percent=None,
            session_resets_at=None, weekly_resets_at=None, detail="req 10 tok 1M",
        )
        scheduler._fetch_one(Provider.UMANS, MagicMock(return_value=r1))
        scheduler._fetch_one(Provider.UMANS, MagicMock(return_value=r1))
        assert _interval(scheduler, Provider.UMANS) == 600

        r2 = _make_reading(
            provider=Provider.UMANS, session_percent=None, weekly_percent=None,
            session_resets_at=None, weekly_resets_at=None, detail="req 11 tok 1.2M",
        )
        scheduler._fetch_one(Provider.UMANS, MagicMock(return_value=r2))
        assert _interval(scheduler, Provider.UMANS) == 300

    def test_boxed_reading_stays_at_floor(self, tmp_path):
        # An unchanged reading normally lets the idle ladder widen the gap, but a
        # boxed (penalty-box) reading must keep polling at the floor so the box
        # clearing is caught on the next scan.
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(db, umans_key="key", interval_seconds=300)
        boxed = _make_reading(
            provider=Provider.UMANS, session_percent=None, weekly_percent=None,
            session_resets_at=datetime(2026, 1, 14, 17, 0, 0), weekly_resets_at=None,
            detail="req 10 tok 1M", throttle=THROTTLE_BOXED,
        )
        # Identical boxed reading three scans running — would widen to 900 if the
        # idle ladder applied; instead it stays pinned at the 300s floor.
        for _ in range(3):
            scheduler._fetch_one(Provider.UMANS, MagicMock(return_value=boxed))
        assert _interval(scheduler, Provider.UMANS) == 300

    def test_low_interactivity_reading_stays_at_floor(self, tmp_path):
        # Same rationale as boxed: an idle account in low-interactivity mode
        # moves no counters, so only the floor pin catches the mode clearing
        # promptly.
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(db, umans_key="key", interval_seconds=300)
        low_interactivity = _make_reading(
            provider=Provider.UMANS, session_percent=None, weekly_percent=None,
            session_resets_at=datetime(2026, 1, 14, 17, 0, 0), weekly_resets_at=None,
            detail="24h req 10 tok 1M", throttle=THROTTLE_LOW_INTERACTIVITY,
        )
        for _ in range(3):
            scheduler._fetch_one(
                Provider.UMANS, MagicMock(return_value=low_interactivity)
            )
        assert _interval(scheduler, Provider.UMANS) == 300

    def test_throttle_change_snaps_back_to_floor(self, tmp_path):
        # A throttle flip with otherwise-identical fields is a real state
        # change and must reset the idle ladder.
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(db, umans_key="key", interval_seconds=300)
        base = dict(
            provider=Provider.UMANS, session_percent=None, weekly_percent=None,
            session_resets_at=None, weekly_resets_at=None, detail="24h req 10 tok 1M",
        )
        normal = _make_reading(**base, throttle=THROTTLE_NONE)
        for _ in range(3):
            scheduler._fetch_one(Provider.UMANS, MagicMock(return_value=normal))
        assert _interval(scheduler, Provider.UMANS) > 300
        low = _make_reading(**base, throttle=THROTTLE_LOW)
        scheduler._fetch_one(Provider.UMANS, MagicMock(return_value=low))
        assert _interval(scheduler, Provider.UMANS) == 300


class TestExponentialFailureBackoff:
    def test_non_429_failure_backs_off_exponentially(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(
            db, claude_token="token", interval_seconds=300, failure_cap_seconds=3600
        )
        fetch_fn = MagicMock(side_effect=FetchError("boom"))  # no explicit 429
        for expected in (300, 600, 1200, 2400):
            scheduler._fetch_one(Provider.CLAUDE, fetch_fn)
            assert _interval(scheduler, Provider.CLAUDE) == expected

    def test_failure_backoff_is_capped(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(
            db, claude_token="token", interval_seconds=300, failure_cap_seconds=1000
        )
        fetch_fn = MagicMock(side_effect=FetchError("boom"))
        for _ in range(8):
            scheduler._fetch_one(Provider.CLAUDE, fetch_fn)
        assert _interval(scheduler, Provider.CLAUDE) == 1000

    def test_recovery_resets_interval_and_failures(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(db, claude_token="token", interval_seconds=300)
        fail_fn = MagicMock(side_effect=FetchError("boom"))
        scheduler._fetch_one(Provider.CLAUDE, fail_fn)
        scheduler._fetch_one(Provider.CLAUDE, fail_fn)
        assert _interval(scheduler, Provider.CLAUDE) == 600

        ok = _make_reading(provider=Provider.CLAUDE)
        scheduler._fetch_one(Provider.CLAUDE, MagicMock(return_value=ok))
        snap = scheduler.schedule_snapshot()[Provider.CLAUDE]
        assert snap["interval_seconds"] == 300
        assert snap["failures"] == 0
        assert db.get_consecutive_failures(Provider.CLAUDE) == 0


class TestPerProviderScheduling:
    def test_intervals_tracked_independently(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(
            db, claude_token="token", zai_key="key", interval_seconds=300
        )
        steady = _make_reading(provider=Provider.CLAUDE)
        scheduler._fetch_one(Provider.CLAUDE, MagicMock(return_value=steady))
        scheduler._fetch_one(Provider.CLAUDE, MagicMock(return_value=steady))

        scheduler._fetch_one(
            Provider.ZAI, MagicMock(side_effect=FetchError("boom"))
        )

        snap = scheduler.schedule_snapshot()
        assert snap[Provider.CLAUDE]["interval_seconds"] == 600  # idle, widened
        assert snap[Provider.ZAI]["interval_seconds"] == 300     # failing, base backoff
        assert snap[Provider.CLAUDE]["last_success"] is not None
        assert snap[Provider.ZAI]["last_success"] is None


class TestConfiguredProviders:
    def test_only_credentialed_providers_are_configured(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(
            db, claude_token="token", ollama_cookie="cookie"
        )
        assert scheduler.configured_providers() == [
            Provider.CLAUDE,
            Provider.OLLAMA,
        ]

    def test_no_credentials_means_no_configured_providers(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(db)
        assert scheduler.configured_providers() == []

    def test_configured_providers_are_in_enum_order(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(
            db,
            umans_key="u",
            claude_token="c",
            claude_work_token="cw",
            zai_key="z",
            ollama_cookie="o",
            codex_token="cx",
        )
        assert scheduler.configured_providers() == list(Provider)


class TestOllamaAuthFailureSignal:
    def test_auth_failure_stores_offline_with_relogin_detail(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(db, ollama_cookie="cookie")
        fetch_fn = MagicMock(side_effect=FetchAuthError("HTTP 401"))
        scheduler._fetch_one(Provider.OLLAMA, fetch_fn)
        reading = db.get_latest_readings()[Provider.OLLAMA]
        assert reading.status is ReadingStatus.OFFLINE
        assert reading.detail is not None
        assert "re-login" in reading.detail

    def test_non_auth_failure_still_walks_stale_ladder(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        original = _make_reading(
            provider=Provider.OLLAMA, status=ReadingStatus.CURRENT, stale=False,
        )
        db.store_reading(original)
        scheduler = FetchScheduler(db, ollama_cookie="cookie")
        fetch_fn = MagicMock(side_effect=FetchError("timeout"))
        scheduler._fetch_one(Provider.OLLAMA, fetch_fn)
        reading = db.get_latest_readings()[Provider.OLLAMA]
        assert reading.status is ReadingStatus.STALE
        assert reading.detail is None

    def test_auth_failure_increments_failures_and_sets_backoff(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(
            db, ollama_cookie="cookie", interval_seconds=300, failure_cap_seconds=3600
        )
        fetch_fn = MagicMock(side_effect=FetchAuthError("HTTP 401"))
        scheduler._fetch_one(Provider.OLLAMA, fetch_fn)
        assert db.get_consecutive_failures(Provider.OLLAMA) == 1
        assert _interval(scheduler, Provider.OLLAMA) == 3600

    def test_repeated_auth_failures_stay_at_cap(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(
            db, ollama_cookie="cookie", interval_seconds=300, failure_cap_seconds=3600
        )
        fetch_fn = MagicMock(side_effect=FetchAuthError("HTTP 401"))
        scheduler._fetch_one(Provider.OLLAMA, fetch_fn)
        scheduler._fetch_one(Provider.OLLAMA, fetch_fn)
        assert db.get_consecutive_failures(Provider.OLLAMA) == 2
        assert _interval(scheduler, Provider.OLLAMA) == 3600
        reading = db.get_latest_readings()[Provider.OLLAMA]
        assert reading.status is ReadingStatus.OFFLINE
        assert "re-login" in reading.detail

    def test_auth_failure_overwrites_prior_current_reading(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        original = _make_reading(
            provider=Provider.OLLAMA, status=ReadingStatus.CURRENT, stale=False,
            session_percent=42.0,
        )
        db.store_reading(original)
        scheduler = FetchScheduler(db, ollama_cookie="cookie")
        fetch_fn = MagicMock(side_effect=FetchAuthError("HTTP 401"))
        scheduler._fetch_one(Provider.OLLAMA, fetch_fn)
        reading = db.get_latest_readings()[Provider.OLLAMA]
        assert reading.status is ReadingStatus.OFFLINE
        assert "re-login" in (reading.detail or "")
        assert reading.session_percent is None

    def test_recovery_after_auth_failure_resets_state(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(
            db, ollama_cookie="cookie", interval_seconds=300, failure_cap_seconds=3600
        )
        scheduler._fetch_one(Provider.OLLAMA, MagicMock(side_effect=FetchAuthError("401")))
        assert db.get_consecutive_failures(Provider.OLLAMA) == 1
        ok = _make_reading(provider=Provider.OLLAMA, session_percent=10.0)
        scheduler._fetch_one(Provider.OLLAMA, MagicMock(return_value=ok))
        assert db.get_consecutive_failures(Provider.OLLAMA) == 0
        assert _interval(scheduler, Provider.OLLAMA) == 300
        assert db.get_latest_readings()[Provider.OLLAMA].status is ReadingStatus.CURRENT

    def test_non_auth_failure_after_auth_failure_clears_relogin_detail(self, tmp_path):
        db = Database(str(tmp_path / "sched.db"))
        db.initialize()
        scheduler = FetchScheduler(db, ollama_cookie="cookie", offline_threshold=99)
        scheduler._fetch_one(Provider.OLLAMA, MagicMock(side_effect=FetchAuthError("401")))
        assert "re-login" in (db.get_latest_readings()[Provider.OLLAMA].detail or "")
        scheduler._fetch_one(Provider.OLLAMA, MagicMock(side_effect=FetchError("timeout")))
        reading = db.get_latest_readings()[Provider.OLLAMA]
        assert reading.status is ReadingStatus.STALE
        assert reading.detail is None
