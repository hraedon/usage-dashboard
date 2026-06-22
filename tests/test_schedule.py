from __future__ import annotations

from datetime import datetime

import pytest

from usage_dashboard.client.schedule import (
    DEFAULT_SCHEDULE_SPEC,
    SleepSchedule,
    SleepWindow,
    default_sleep_schedule,
    parse_schedule,
)

# Reference week: Mon 2026-06-22 .. Sun 2026-06-28 (weekday() 0..6).
MON = datetime(2026, 6, 22)  # a Monday
TUE = datetime(2026, 6, 23)
FRI = datetime(2026, 6, 26)
SAT = datetime(2026, 6, 27)
SUN = datetime(2026, 6, 28)


def _dt(base: datetime, h: int, m: int = 0) -> datetime:
    return base.replace(hour=h, minute=m)


class TestDefaultScheduleAsleep:
    def setup_method(self) -> None:
        self.s = default_sleep_schedule()

    def test_weeknight_window_is_asleep(self) -> None:
        assert self.s.is_asleep(_dt(TUE, 2, 0)) is True   # 02:00 Tue
        assert self.s.is_asleep(_dt(TUE, 7, 59)) is True
        assert self.s.is_asleep(_dt(TUE, 8, 0)) is False  # wakes at 08:00
        assert self.s.is_asleep(_dt(TUE, 0, 0)) is True   # sleeps at midnight

    def test_weekday_daytime_is_awake(self) -> None:
        assert self.s.is_asleep(_dt(TUE, 12, 0)) is False
        assert self.s.is_asleep(_dt(TUE, 23, 59)) is False

    def test_friday_evening_starts_weekend_sleep(self) -> None:
        assert self.s.is_asleep(_dt(FRI, 17, 59)) is False
        assert self.s.is_asleep(_dt(FRI, 18, 0)) is True

    def test_saturday_all_day_asleep(self) -> None:
        assert self.s.is_asleep(_dt(SAT, 2, 0)) is True
        assert self.s.is_asleep(_dt(SAT, 14, 0)) is True
        assert self.s.is_asleep(_dt(SUN, 14, 0)) is True

    def test_monday_morning_wakes_for_the_week(self) -> None:
        assert self.s.is_asleep(_dt(MON, 7, 59)) is True
        assert self.s.is_asleep(_dt(MON, 8, 0)) is False


class TestTapToWakeRule:
    """The worked examples from plans/002."""

    def setup_method(self) -> None:
        self.s = default_sleep_schedule()

    def test_friday_8pm_wakes_until_friday_midnight(self) -> None:
        # In the weekend block; next midnight (Sat 00:00) is earlier than the
        # block's natural end (Mon 08:00), so the tap holds until Friday midnight.
        assert self.s.wake_until(_dt(FRI, 20, 0)) == _dt(SAT, 0, 0)

    def test_saturday_2pm_wakes_until_sunday_midnight(self) -> None:
        assert self.s.wake_until(_dt(SAT, 14, 0)) == _dt(SUN, 0, 0)

    def test_tuesday_2am_wakes_until_natural_8am(self) -> None:
        # In the nightly window; the window end (08:00) is earlier than next
        # midnight, so no 22-hour-on surprise.
        assert self.s.wake_until(_dt(TUE, 2, 0)) == _dt(TUE, 8, 0)

    def test_wake_until_none_when_awake(self) -> None:
        assert self.s.wake_until(_dt(TUE, 12, 0)) is None

    def test_current_window_end_merges_weekend_block(self) -> None:
        # The contiguous weekend sleep period runs to the *following* Mon 08:00
        # (2026-06-29), even though nightly windows are nested inside it.
        assert self.s.current_window_end(_dt(SAT, 14, 0)) == datetime(2026, 6, 29, 8, 0)


class TestParseSchedule:
    def test_default_spec_matches_programmatic_default(self) -> None:
        # The spec-parsed default must behave identically to the fleet default
        # across a representative set of moments.
        s = parse_schedule(DEFAULT_SCHEDULE_SPEC)
        d = default_sleep_schedule()
        for moment in (
            _dt(TUE, 2), _dt(TUE, 8), _dt(TUE, 12),
            _dt(FRI, 17, 59), _dt(FRI, 18), _dt(SAT, 14), _dt(MON, 7, 59), _dt(MON, 8),
        ):
            assert s.is_asleep(moment) == d.is_asleep(moment)
            assert s.wake_until(moment) == d.wake_until(moment)

    def test_daily_window(self) -> None:
        s = parse_schedule("daily 00:00-08:00")
        assert s.is_asleep(_dt(TUE, 3)) is True
        assert s.is_asleep(_dt(SAT, 3)) is True   # every day
        assert s.is_asleep(_dt(TUE, 9)) is False

    def test_daily_window_crossing_midnight(self) -> None:
        s = parse_schedule("daily 22:00-06:00")
        assert s.is_asleep(_dt(TUE, 23)) is True
        assert s.is_asleep(_dt(TUE, 2)) is True    # spilled from Monday night
        assert s.is_asleep(_dt(TUE, 12)) is False

    def test_span_rule(self) -> None:
        s = parse_schedule("fri 18:00-mon 08:00")
        assert s.is_asleep(_dt(SAT, 12)) is True
        assert s.is_asleep(_dt(FRI, 12)) is False
        assert s.is_asleep(_dt(MON, 9)) is False

    def test_whitespace_and_empty_rules_tolerated(self) -> None:
        s = parse_schedule(" daily 00:00-08:00 ;  ; ")
        assert s.is_asleep(_dt(TUE, 3)) is True

    @pytest.mark.parametrize("spec", [
        "daily 25:00-08:00",   # bad hour
        "daily 00:00",         # missing range
        "funday 1-2",          # unknown day
        "fri 18:00-08:00",     # span missing end day
    ])
    def test_malformed_raises_value_error(self, spec: str) -> None:
        with pytest.raises(ValueError):
            parse_schedule(spec)


class TestSimpleWindow:
    def test_seconds_truncated_in_window_end(self) -> None:
        s = SleepSchedule([SleepWindow(0, 8 * 60)])  # Mon 00:00-08:00 only
        end = s.current_window_end(MON.replace(hour=2, minute=0, second=37))
        assert end == _dt(MON, 8, 0)

    def test_non_wrapping_single_window(self) -> None:
        s = SleepSchedule([SleepWindow(0, 8 * 60)])
        assert s.is_asleep(_dt(MON, 3, 0)) is True
        assert s.is_asleep(_dt(TUE, 3, 0)) is False  # only Monday
