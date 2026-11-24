"""The readings cache: the worker publishes, the GUI reads, and a payload is
never lost or seen half-written in between (Plan 010).

The contract under test is the plan's "preserve the last successful payload
and its timestamp when both routes fail". That only holds if a publish is
indivisible, if a damaged file is refused rather than half-rendered, and if
the GUI can tell a new payload from one it has already looked at without
re-parsing a file ten times a second.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from usage_dashboard.client import readings_cache as cache
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus

_NOW = datetime(2026, 11, 20, 9, 30, 0)  # naive UTC, as readings use


def _reading(provider: Provider = Provider.CLAUDE) -> dict[str, object]:
    return Reading(
        provider, ReadingStatus.CURRENT, 40.0, _NOW, 55.0, _NOW, _NOW, False,
    ).to_dict()


def _entry(
    source: str = cache.SOURCE_CELLULAR,
    written_at: datetime = _NOW,
    raw: list[dict[str, object]] | None = None,
    poll_interval: int = 600,
) -> cache.CacheEntry:
    return cache.CacheEntry(
        written_at=written_at,
        source=source,
        poll_interval=poll_interval,
        raw=raw if raw is not None else [_reading()],
    )


def _leftovers(state_dir: Path) -> list[str]:
    return [p.name for p in state_dir.iterdir() if p.name.endswith(".tmp")]


class TestRoundTrip:
    def test_publish_then_load_gives_back_the_same_entry(self, tmp_path: Path) -> None:
        entry = _entry(raw=[_reading(), _reading(Provider.ZAI)])
        cache.publish(entry, tmp_path)
        loaded = cache.load(tmp_path)
        assert loaded == entry

    def test_the_cache_file_is_readable_by_another_user(self, tmp_path: Path) -> None:
        # The GUI runs as a different user than the worker and must be able to
        # read what the worker wrote.
        cache.publish(_entry(), tmp_path)
        if os.name != "nt":
            assert ((tmp_path / cache.READINGS_FILE).stat().st_mode & 0o777) & 0o004

    def test_unknown_providers_are_skipped_but_the_rest_survive(self, tmp_path: Path) -> None:
        # A server can ship a new provider before every panel has updated.
        raw = [
            {"provider": "provider-from-the-future", "status": "current"},
            _reading(),
            _reading(Provider.ZAI),
        ]
        cache.publish(_entry(raw=raw), tmp_path)
        loaded = cache.load(tmp_path)
        assert loaded is not None
        assert [r.provider for r in loaded.readings()] == [Provider.CLAUDE, Provider.ZAI]
        # ...and the payload is preserved verbatim, so a newer GUI can still use it.
        assert loaded.raw == raw

    def test_empty_snapshot_is_valid(self, tmp_path: Path) -> None:
        cache.publish(_entry(raw=[]), tmp_path)
        loaded = cache.load(tmp_path)
        assert loaded is not None and loaded.readings() == []

    def test_timestamp_accepts_z_and_explicit_offsets(self) -> None:
        assert cache.parse_ts("2026-11-20T09:30:00Z", "t") == _NOW
        assert cache.parse_ts("2026-11-20T10:30:00+01:00", "t") == _NOW
        assert cache.parse_ts("2026-11-20T09:30:00+00:00", "t") == _NOW
        with pytest.raises(cache.CacheFormatError):
            cache.parse_ts("yesterday", "t")
        with pytest.raises(cache.CacheFormatError):
            cache.parse_ts(1763635800, "t")

    def test_format_ts_emits_utc_z(self) -> None:
        aware = datetime(2026, 11, 20, 10, 30, 0, tzinfo=timezone(timedelta(hours=1)))
        assert cache.format_ts(aware) == "2026-11-20T09:30:00Z"
        assert cache.format_ts(_NOW) == "2026-11-20T09:30:00Z"


class TestDamagedFilesAreRefused:
    def test_missing_file_is_none(self, tmp_path: Path) -> None:
        assert cache.load(tmp_path) is None

    @pytest.mark.parametrize(
        "text",
        [
            "not json at all",
            "[]",
            '{"schema": "usage-dashboard-readings-cache-v2", "readings": []}',
            '{"schema": "usage-dashboard-readings-cache-v1"}',
            '{"schema": "usage-dashboard-readings-cache-v1", "written_at": "2026-11-20T09:30:00Z",'
            ' "source": "wifi", "poll_interval": 600, "readings": []}',
            '{"schema": "usage-dashboard-readings-cache-v1", "written_at": "2026-11-20T09:30:00Z",'
            ' "source": "cellular", "poll_interval": 0, "readings": []}',
            '{"schema": "usage-dashboard-readings-cache-v1", "written_at": "2026-11-20T09:30:00Z",'
            ' "source": "cellular", "poll_interval": 600, "readings": {}}',
            # Structurally right, but a reading the GUI cannot decode: better
            # no data than a panel that throws mid-frame.
            '{"schema": "usage-dashboard-readings-cache-v1", "written_at": "2026-11-20T09:30:00Z",'
            ' "source": "cellular", "poll_interval": 600, "readings": [{"provider": "claude"}]}',
        ],
    )
    def test_unusable_content_is_rejected_whole(self, tmp_path: Path, text: str) -> None:
        (tmp_path / cache.READINGS_FILE).write_text(text)
        assert cache.load(tmp_path) is None

    def test_a_bad_timestamp_is_not_replaced_by_now(self) -> None:
        # If an unparseable written_at silently became "now", an old payload
        # would read as fresh — the one failure the age display exists for.
        with pytest.raises(cache.CacheFormatError):
            cache.decode_cache(
                '{"schema": "usage-dashboard-readings-cache-v1", "written_at": "",'
                ' "source": "cellular", "poll_interval": 600, "readings": []}'
            )


class TestAtomicPublish:
    def test_repeated_publishes_leave_no_temp_files(self, tmp_path: Path) -> None:
        for second in range(20):
            cache.publish(
                _entry(written_at=_NOW + timedelta(seconds=second)), tmp_path
            )
        assert _leftovers(tmp_path) == []
        assert cache.load(tmp_path) is not None

    def test_a_failed_write_leaves_the_previous_payload_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = _entry()
        cache.publish(first, tmp_path)
        monkeypatch.setattr(cache.os, "fsync", _boom)
        with pytest.raises(OSError):
            cache.publish(_entry(written_at=_NOW + timedelta(hours=3)), tmp_path)
        assert cache.load(tmp_path) == first
        assert _leftovers(tmp_path) == []

    def test_a_crash_mid_write_is_not_observable_as_the_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Whatever is in the temporary file before the rename is never what a
        # reader sees: readers open the fixed name, which still points at the
        # old file until the rename lands.
        first = _entry()
        cache.publish(first, tmp_path)
        real_replace = cache.os.replace
        seen: list[cache.CacheEntry | None] = []

        def replace_after_a_look(src: str, dst: str) -> None:
            seen.append(cache.load(tmp_path))
            real_replace(src, dst)

        monkeypatch.setattr(cache.os, "replace", replace_after_a_look)
        cache.publish(_entry(written_at=_NOW + timedelta(hours=1)), tmp_path)
        assert seen == [first]
        assert cache.load(tmp_path) is not None
        assert cache.load(tmp_path) != first


def _boom(fd: int) -> None:
    raise OSError("disk went away")


class TestCacheReader:
    def test_reloads_only_when_the_file_changes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reader = cache.CacheReader(tmp_path)
        assert reader.latest() is None
        calls = []
        real_load = cache.load
        monkeypatch.setattr(cache, "load", lambda d: calls.append(1) or real_load(d))

        cache.publish(_entry(raw=[_reading()]), tmp_path)
        first = reader.latest()
        assert first is not None and len(first.raw) == 1
        for _ in range(5):  # the GUI asks every frame
            assert reader.latest() is first
        assert len(calls) == 1

        cache.publish(_entry(raw=[_reading(), _reading(Provider.ZAI)]), tmp_path)
        second = reader.latest()
        assert second is not None and len(second.raw) == 2
        assert second is not first
        assert len(calls) == 2

    def test_a_file_that_disappears_clears_what_was_shown(self, tmp_path: Path) -> None:
        reader = cache.CacheReader(tmp_path)
        cache.publish(_entry(), tmp_path)
        assert reader.latest() is not None
        (tmp_path / cache.READINGS_FILE).unlink()
        assert reader.latest() is None

    def test_an_unusable_file_is_not_reparsed_every_frame(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / cache.READINGS_FILE).write_text("{broken")
        reader = cache.CacheReader(tmp_path)
        assert reader.latest() is None
        calls = []
        real_load = cache.load
        monkeypatch.setattr(cache, "load", lambda d: calls.append(1) or real_load(d))
        for _ in range(10):
            assert reader.latest() is None
        assert calls == []


class TestWorkerStatus:
    def test_round_trip(self, tmp_path: Path) -> None:
        status = cache.WorkerStatus(
            updated_at=_NOW,
            state="fallback",
            next_poll_at=_NOW + timedelta(minutes=10),
            poll_interval=600,
            last_ok_at=_NOW - timedelta(minutes=1),
            last_source=cache.SOURCE_CELLULAR,
            normal_error="timeout",
            cellular_error="",
            month="2026-11",
            bytes_used=1234,
            budget_bytes=50_000_000,
            request=cache.RequestOutcome(1.5, "deferred"),
        )
        cache.write_status(status, tmp_path)
        assert cache.read_status(tmp_path) == status

    def test_optional_fields_may_be_absent(self, tmp_path: Path) -> None:
        status = cache.WorkerStatus(
            updated_at=_NOW, state="idle", next_poll_at=None, poll_interval=600,
            last_ok_at=None, last_source=None, normal_error="", cellular_error="timeout",
            month="2026-11", bytes_used=0, budget_bytes=50_000_000,
        )
        cache.write_status(status, tmp_path)
        assert cache.read_status(tmp_path) == status

    @pytest.mark.parametrize("text", ["", "{", "[]", '{"schema": "other"}'])
    def test_unusable_status_is_none(self, tmp_path: Path, text: str) -> None:
        (tmp_path / cache.STATUS_FILE).write_text(text)
        assert cache.read_status(tmp_path) is None

    def test_missing_status_is_none(self, tmp_path: Path) -> None:
        assert cache.read_status(tmp_path) is None


class TestGuiSignals:
    def test_no_heartbeat_means_asleep(self, tmp_path: Path) -> None:
        assert cache.is_awake(tmp_path, now=1000.0, timeout=150.0) is False

    def test_fresh_heartbeat_is_awake_and_it_expires(self, tmp_path: Path) -> None:
        cache.touch_awake(tmp_path)
        mtime = (tmp_path / cache.AWAKE_FILE).stat().st_mtime
        assert cache.is_awake(tmp_path, now=mtime + 10, timeout=150.0) is True
        assert cache.is_awake(tmp_path, now=mtime + 151, timeout=150.0) is False

    def test_a_heartbeat_from_the_future_is_asleep(self, tmp_path: Path) -> None:
        # A Pi that boots before its clock is set can step backwards, leaving
        # a heartbeat that would otherwise never age out.
        cache.touch_awake(tmp_path)
        mtime = (tmp_path / cache.AWAKE_FILE).stat().st_mtime
        assert cache.is_awake(tmp_path, now=mtime - 3600, timeout=150.0) is False

    def test_going_dark_takes_effect_at_once(self, tmp_path: Path) -> None:
        cache.touch_awake(tmp_path)
        mtime = (tmp_path / cache.AWAKE_FILE).stat().st_mtime
        cache.clear_awake(tmp_path)
        assert cache.is_awake(tmp_path, now=mtime + 1, timeout=150.0) is False
        # Idempotent: clearing an already-cleared signal is not an error.
        cache.clear_awake(tmp_path)

    def test_refresh_request_round_trips_a_token(self, tmp_path: Path) -> None:
        assert cache.read_refresh_token(tmp_path) is None
        assert cache.request_refresh(tmp_path, 1763635800.25) is True
        assert cache.read_refresh_token(tmp_path) == 1763635800.25
        assert cache.request_refresh(tmp_path, 1763635900.5) is True
        assert cache.read_refresh_token(tmp_path) == 1763635900.5

    def test_garbled_refresh_token_is_none(self, tmp_path: Path) -> None:
        (tmp_path / cache.REFRESH_FILE).write_text("soon please")
        assert cache.read_refresh_token(tmp_path) is None
