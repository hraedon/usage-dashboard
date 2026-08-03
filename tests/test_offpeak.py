from __future__ import annotations

from datetime import datetime, timezone

import pytest

from usage_dashboard.shared.offpeak import (
    qwen_is_offpeak,
    qwen_peak_countdown,
    zai_is_peak,
    zai_peak_countdown,
)


def _utc(year, month, day, hour, minute=0, second=0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


class TestZaiPeak:
    @pytest.mark.parametrize("hour", [14, 15, 16, 17])
    def test_weekday_afternoon_is_peak(self, hour) -> None:
        # Mon 2026-01-12 (a Monday); 14:00–18:00 Singapore = 06:00–10:00 UTC.
        assert zai_is_peak(_utc(2026, 1, 12, hour - 8)) is True

    def test_weekday_boundaries(self) -> None:
        # 13:59 Singapore (05:59 UTC) is off-peak; 18:00 (10:00 UTC) is not peak.
        assert zai_is_peak(_utc(2026, 1, 12, 5, 59)) is False
        assert zai_is_peak(_utc(2026, 1, 12, 10, 0)) is False

    @pytest.mark.parametrize("hour", [0, 6, 12, 18, 23])
    def test_weekday_outside_peak_is_offpeak(self, hour) -> None:
        # Local UTC+8 hour -> UTC hour is local-8.
        local_hour = (hour - 8) % 24
        assert zai_is_peak(_utc(2026, 1, 12, local_hour)) is False

    @pytest.mark.parametrize("day", [10, 11, 17, 18])  # Sat/Sun pairs (2026-01)
    def test_weekend_is_never_peak(self, day) -> None:
        # Even inside the 14:00–18:00 slot, weekends are off-peak.
        assert zai_is_peak(_utc(2026, 1, day, 6, 0)) is False  # 14:00 UTC+8

    def test_naive_datetime_treated_as_utc(self) -> None:
        # Naive-UTC (codebase convention) == aware-UTC for the same instant.
        assert zai_is_peak(datetime(2026, 1, 12, 7, 0)) is True  # Mon 15:00 UTC+8


class TestQwenOffpeak:
    @pytest.mark.parametrize("utc_hour", [14, 16, 18, 22, 23])  # 22:00–07:59 UTC+8
    def test_utc_evening_is_offpeak(self, utc_hour) -> None:
        # 14:00 UTC = 22:00 UTC+8 (off-peak start) through 23:59 UTC =
        # 07:59 UTC+8 the next day — one contiguous window in UTC.
        assert qwen_is_offpeak(_utc(2026, 1, 12, utc_hour)) is True

    @pytest.mark.parametrize("utc_hour", [0, 6, 12, 13])  # 08:00–21:59 UTC+8
    def test_utc_morning_noon_is_peak(self, utc_hour) -> None:
        # 00:00 UTC = 08:00 UTC+8 (peak start) through 13:59 UTC = 21:59 UTC+8.
        assert qwen_is_offpeak(_utc(2026, 1, 12, utc_hour)) is False

    def test_boundaries(self) -> None:
        assert qwen_is_offpeak(_utc(2026, 1, 12, 14, 0)) is True  # 22:00 UTC+8
        assert qwen_is_offpeak(_utc(2026, 1, 12, 13, 59)) is False  # 21:59 UTC+8
        assert qwen_is_offpeak(_utc(2026, 1, 12, 0, 0)) is False  # 08:00 UTC+8
        assert qwen_is_offpeak(_utc(2026, 1, 12, 23, 59)) is True  # 07:59 UTC+8 next day


class TestZaiPeakCountdown:
    def test_offpeak_counts_down_to_next_peak_start(self) -> None:
        # Mon 2026-01-12 10:00 UTC = Mon 18:00 UTC+8 (off-peak); next peak
        # starts Tue 14:00 UTC+8 = Tue 06:00 UTC -> 20 hours.
        pc = zai_peak_countdown(_utc(2026, 1, 12, 10, 0))
        assert pc.in_peak is False
        assert pc.seconds_to_boundary == 20 * 3600

    def test_in_peak_counts_down_to_peak_end(self) -> None:
        # Mon 07:00 UTC = 15:00 UTC+8 (in peak); the window closes 18:00 UTC+8
        # (= 10:00 UTC) -> 3 hours.
        pc = zai_peak_countdown(_utc(2026, 1, 12, 7, 0))
        assert pc.in_peak is True
        assert pc.seconds_to_boundary == 3 * 3600

    def test_friday_evening_counts_down_to_monday_peak(self) -> None:
        # Fri 2026-01-16 11:00 UTC = 19:00 UTC+8; next boundary is Mon 14:00
        # UTC+8 (= Mon 06:00 UTC) -> 67 hours.
        pc = zai_peak_countdown(_utc(2026, 1, 16, 11, 0))
        assert pc.in_peak is False
        assert pc.seconds_to_boundary == 67 * 3600

    def test_weekend_counts_down_to_monday_peak(self) -> None:
        # Sat 2026-01-17 20:00 UTC -> Mon 06:00 UTC -> 34 hours.
        pc = zai_peak_countdown(_utc(2026, 1, 17, 20, 0))
        assert pc.in_peak is False
        assert pc.seconds_to_boundary == 34 * 3600

    def test_boundary_strictly_after_now(self) -> None:
        # Exactly 13:59:59 UTC+8 (= 05:59:59 UTC): the peak starts one second
        # later, at Mon 14:00 UTC+8 (= 06:00 UTC).
        pc = zai_peak_countdown(_utc(2026, 1, 12, 5, 59, 59))
        assert pc.in_peak is False
        assert pc.seconds_to_boundary == 1

    # PeakWindow round-trip sanity: in_peak / seconds are consistent for the
    # four cardinal cases exercised above; nothing to store, so no round-trip.


class TestQwenPeakCountdown:
    def test_offpeak_counts_down_to_peak_start(self) -> None:
        # 23:30 UTC = 07:30 UTC+8 next day (off-peak); peak starts 08:00 UTC+8
        # (= 00:00 UTC) -> 30 minutes.
        pc = qwen_peak_countdown(_utc(2026, 1, 12, 23, 30))
        assert pc.in_peak is False
        assert pc.seconds_to_boundary == 30 * 60

    def test_in_peak_counts_down_to_peak_end(self) -> None:
        # 12:00 UTC = 20:00 UTC+8 (peak); the window closes 22:00 UTC+8
        # (= 14:00 UTC) -> 2 hours.
        pc = qwen_peak_countdown(_utc(2026, 1, 12, 12, 0))
        assert pc.in_peak is True
        assert pc.seconds_to_boundary == 2 * 3600

    def test_offpeak_crosses_midnight(self) -> None:
        # Sun 2026-01-11 20:00 UTC = Mon 04:00 UTC+8 (off-peak); peak starts
        # Mon 08:00 UTC+8 (= Mon 00:00 UTC) -> 4 hours.
        pc = qwen_peak_countdown(_utc(2026, 1, 11, 20, 0))
        assert pc.in_peak is False
        assert pc.seconds_to_boundary == 4 * 3600
