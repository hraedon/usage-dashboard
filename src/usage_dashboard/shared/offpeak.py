"""Off-peak window helpers for subscription plans that discount off-peak use.

Pure time-window functions shared by the Pi client and the web dashboard so the
two never drift on what "off-peak" means. Windows are defined in the vendors'
home timezone (UTC+8) and converted to the caller's clock — the Pi's local time
is irrelevant to which side of a UTC+8 window it is.

z.ai (GLM Coding Plan): peak is Monday–Friday 14:00–18:00 Singapore time
(UTC+8); outside that window usage is off-peak (quota burns at a discount).
Qwen token plan: off-peak is 22:00–08:00 daily UTC+8 (credits consume much
less), i.e. peak is 08:00–22:00 daily UTC+8.

The countdown helpers give the time until the *next* boundary — peak start
when currently off-peak, peak end when currently in peak — so clients can
render "peak in 3h 24m" / "ends in 1h 24m" next to the existing off-peak tint.

Datetimes are expected naive-UTC (the codebase-wide convention) or aware; both
are handled. ``None`` means "right now".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from usage_dashboard.shared.format import format_duration

_UTC8 = timezone(timedelta(hours=8))

# z.ai peak window (Mon–Fri 14:00–18:00 Singapore time).
_ZAI_PEAK_START_HOUR = 14
_ZAI_PEAK_END_HOUR = 18  # exclusive
_ZAI_PEAK_DAYS = (0, 1, 2, 3, 4)  # Monday..Friday (Python weekday)

# Qwen token plan off-peak window (22:00–08:00 daily UTC+8); peak is the
# complement (08:00–22:00 daily UTC+8).
_QWEN_OFFPEAK_START_HOUR = 22
_QWEN_OFFPEAK_END_HOUR = 8  # next day, exclusive
_QWEN_PEAK_START_HOUR = _QWEN_OFFPEAK_END_HOUR
_QWEN_PEAK_END_HOUR = _QWEN_OFFPEAK_START_HOUR


@dataclass(frozen=True, slots=True)
class PeakWindow:
    """Current peak state plus the time until the next window boundary.

    ``seconds_to_boundary`` is real elapsed seconds to: the end of the peak
    window when ``in_peak`` is True, or to the next peak start when False.
    """

    in_peak: bool
    seconds_to_boundary: float


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


def _next_or_same_utc8(now_utc8: datetime, hour: int) -> datetime:
    """The next occurrence of *hour* UTC+8 strictly after *now_utc8*."""
    candidate = now_utc8.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now_utc8:
        candidate += timedelta(days=1)
    return candidate


def _next_weekday_utc8(now_utc8: datetime, hour: int) -> datetime:
    """The next Mon–Fri *hour* UTC+8 strictly after *now_utc8*."""
    candidate = _next_or_same_utc8(now_utc8, hour)
    while candidate.weekday() >= 5:  # Saturday/Sunday: roll to Monday 14:00
        candidate += timedelta(days=1)
        candidate = candidate.replace(hour=hour, minute=0, second=0, microsecond=0)
    return candidate


def zai_peak_countdown(now: datetime | None = None) -> PeakWindow:
    """Peak state + seconds until the next z.ai boundary.

    Off-peak: boundary is the next weekday 14:00 UTC+8 (peak start).
    In peak: boundary is today's 18:00 UTC+8 (peak end).
    """
    local = _to_utc8(now)
    if zai_is_peak(local):
        boundary = local.replace(
            hour=_ZAI_PEAK_END_HOUR, minute=0, second=0, microsecond=0
        )
        return PeakWindow(in_peak=True, seconds_to_boundary=(boundary - local).total_seconds())
    boundary = _next_weekday_utc8(local, _ZAI_PEAK_START_HOUR)
    return PeakWindow(in_peak=False, seconds_to_boundary=(boundary - local).total_seconds())


def qwen_peak_countdown(now: datetime | None = None) -> PeakWindow:
    """Peak state + seconds until the next Qwen boundary.

    Off-peak (22:00–08:00 UTC+8): boundary is the next 08:00 (peak start).
    In peak (08:00–22:00): boundary is the next 22:00 (peak end).
    """
    local = _to_utc8(now)
    if qwen_is_offpeak(local):
        boundary = _next_or_same_utc8(local, _QWEN_PEAK_START_HOUR)
        return PeakWindow(in_peak=False, seconds_to_boundary=(boundary - local).total_seconds())
    boundary = _next_or_same_utc8(local, _QWEN_PEAK_END_HOUR)
    return PeakWindow(in_peak=True, seconds_to_boundary=(boundary - local).total_seconds())


def peak_label(window: PeakWindow) -> str:
    """The user-facing countdown text for a peak window.

    Both surfaces render this string, so neither can answer "is it peak?"
    without also answering "for how long?". Keeping the *wording* here (not
    just the arithmetic) is the point: the peak maths was already shared when
    the z.ai countdown shipped on the panel and never reached the web view —
    what diverged was the text built on top of it (WI-030, WI-020 before it).
    """
    return (
        f"peak in {format_duration(window.seconds_to_boundary)}"
        if not window.in_peak
        else f"ends in {format_duration(window.seconds_to_boundary)}"
    )


def zai_peak_label(now: datetime | None = None) -> str:
    """z.ai peak countdown text, e.g. 'peak in 3h 24m' / 'ends in 1h 24m'."""
    return peak_label(zai_peak_countdown(now))


def qwen_peak_label(now: datetime | None = None) -> str:
    """Qwen peak countdown text. Callers prefix the provider name themselves
    (the Pi's status bar writes 'QWEN <label>'; the web card's header already
    says QWEN)."""
    return peak_label(qwen_peak_countdown(now))
