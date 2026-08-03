"""Off-peak window helpers for subscription plans that discount off-peak use.

Pure time-window functions shared by the Pi client and the web dashboard so the
two never drift on what "off-peak" means. Windows are defined in the vendors'
home timezone (UTC+8) and converted to the caller's clock — the Pi's local time
is irrelevant to which side of a UTC+8 window it is.

z.ai (GLM Coding Plan): peak is Monday–Friday 14:00–18:00 Singapore time
(UTC+8); outside that window usage is off-peak (quota burns at a discount).
Qwen token plan: off-peak is 22:00–08:00 daily UTC+8 (credits consume much
less).

Datetimes are expected naive-UTC (the codebase-wide convention) or aware; both
are handled. ``None`` means "right now".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

_UTC8 = timezone(timedelta(hours=8))

# z.ai peak window (Mon–Fri 14:00–18:00 Singapore time).
_ZAI_PEAK_START_HOUR = 14
_ZAI_PEAK_END_HOUR = 18  # exclusive
_ZAI_PEAK_DAYS = (0, 1, 2, 3, 4)  # Monday..Friday (Python weekday)

# Qwen token plan off-peak window (22:00–08:00 daily UTC+8).
_QWEN_OFFPEAK_START_HOUR = 22
_QWEN_OFFPEAK_END_HOUR = 8  # next day, exclusive


def _to_utc8(now: datetime | None) -> datetime:
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(_UTC8)


def zai_is_peak(now: datetime | None = None) -> bool:
    """True when *now* falls inside z.ai's peak window (Mon–Fri 14:00–18:00
    Singapore time)."""
    local = _to_utc8(now)
    return (
        local.weekday() in _ZAI_PEAK_DAYS
        and _ZAI_PEAK_START_HOUR <= local.hour < _ZAI_PEAK_END_HOUR
    )


def zai_is_offpeak(now: datetime | None = None) -> bool:
    return not zai_is_peak(now)


def qwen_is_offpeak(now: datetime | None = None) -> bool:
    """True when *now* falls inside the Qwen token plan's off-peak window
    (22:00–08:00 daily, UTC+8)."""
    local = _to_utc8(now)
    return local.hour >= _QWEN_OFFPEAK_START_HOUR or local.hour < _QWEN_OFFPEAK_END_HOUR
