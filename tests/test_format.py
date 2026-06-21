from __future__ import annotations

from datetime import datetime, timedelta, timezone

from usage_dashboard.client.format import (
    BAR_BG,
    GRAY,
    GREEN,
    ORANGE,
    RED,
    bar_color,
    format_countdown,
    format_interval,
    mute,
    percent_text,
    status_suffix,
    to_local,
)
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus


def _reading(**over: object) -> Reading:
    base = {
        "provider": Provider.CLAUDE,
        "status": ReadingStatus.CURRENT,
        "session_percent": 10.0,
        "session_resets_at": None,
        "weekly_percent": 20.0,
        "weekly_resets_at": None,
        "fetched_at": datetime(2026, 1, 1),
        "stale": False,
    }
    base.update(over)
    return Reading(**base)  # type: ignore[arg-type]


class TestBarColor:
    def test_green_below_warn(self) -> None:
        assert bar_color(74.9) == GREEN

    def test_orange_at_warn(self) -> None:
        assert bar_color(75.0) == ORANGE

    def test_red_at_crit(self) -> None:
        assert bar_color(85.0) == RED

    def test_gray_when_none(self) -> None:
        assert bar_color(None) == GRAY


class TestFormatCountdown:
    _NOW = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)

    def test_none(self) -> None:
        assert format_countdown(None, now=self._NOW) == ("", False)

    def test_past_is_zero_and_highlighted(self) -> None:
        past = self._NOW - timedelta(hours=1)
        assert format_countdown(past, now=self._NOW) == ("0m", True)

    def test_hours_minutes(self) -> None:
        target = self._NOW + timedelta(hours=2, minutes=30)
        text, hl = format_countdown(target, now=self._NOW)
        assert text == "2h 30m"
        assert hl is True

    def test_days_hours(self) -> None:
        target = self._NOW + timedelta(days=2, hours=3)
        text, hl = format_countdown(target, now=self._NOW)
        assert text == "2d 3h"
        assert hl is True

    def test_beyond_three_days_not_highlighted(self) -> None:
        target = self._NOW + timedelta(days=5)
        _text, hl = format_countdown(target, now=self._NOW)
        assert hl is False

    def test_naive_now_treated_as_utc(self) -> None:
        target = self._NOW + timedelta(hours=1)
        text, _ = format_countdown(target, now=self._NOW.replace(tzinfo=None))
        assert text == "1h 0m"


class TestStatusSuffix:
    def test_current_empty(self) -> None:
        assert status_suffix(_reading(status=ReadingStatus.CURRENT)) == ""

    def test_offline(self) -> None:
        assert status_suffix(_reading(status=ReadingStatus.OFFLINE)) == " [offline]"

    def test_stale_status(self) -> None:
        assert status_suffix(_reading(status=ReadingStatus.STALE)) == " [stale]"

    def test_stale_flag(self) -> None:
        assert status_suffix(_reading(stale=True)) == " [stale]"

    def test_offline_wins_over_stale_flag(self) -> None:
        assert status_suffix(
            _reading(status=ReadingStatus.OFFLINE, stale=True)
        ) == " [offline]"


class TestPercentText:
    def test_value(self) -> None:
        assert percent_text(42.4) == "42%"

    def test_none(self) -> None:
        assert percent_text(None) == "N/A"


class TestMute:
    def test_zero_amount_is_unchanged(self) -> None:
        assert mute(GREEN, 0.0) == GREEN

    def test_full_amount_is_track_gray(self) -> None:
        assert mute(GREEN, 1.0) == BAR_BG

    def test_default_blends_toward_gray_but_keeps_hue(self) -> None:
        muted = mute(GREEN)
        assert muted != GREEN
        # Green channel still dominant over red/blue: hue is still legible.
        assert muted[1] > muted[0] and muted[1] > muted[2]


class TestToLocal:
    def test_converts_utc_to_system_tz(self) -> None:
        utc_naive = datetime(2026, 6, 21, 12, 0, 0)
        local = to_local(utc_naive)
        aware_utc = utc_naive.replace(tzinfo=timezone.utc)
        system_offset = datetime.now().astimezone().utcoffset()
        if system_offset is not None:
            expected = aware_utc + system_offset
            assert local.replace(tzinfo=None) == expected.replace(tzinfo=None)

    def test_preserves_wall_clock_when_utc(self) -> None:
        # If the system tz IS utc, the time should be unchanged.
        utc_naive = datetime(2026, 6, 21, 12, 0, 0)
        local = to_local(utc_naive)
        system_offset = datetime.now().astimezone().utcoffset()
        if system_offset == timedelta(0):
            assert local.hour == 12


class TestFormatInterval:
    def test_seconds(self) -> None:
        assert format_interval(30) == "30s"

    def test_one_minute(self) -> None:
        assert format_interval(60) == "1m"

    def test_five_minutes(self) -> None:
        assert format_interval(300) == "5m"
