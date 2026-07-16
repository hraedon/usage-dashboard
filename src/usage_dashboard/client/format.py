"""Shared display-formatting helpers.

Pure functions used by the pygame touch GUI (Pi 4B + 5" display). Keeping
the colour/threshold and countdown logic here is the single source of truth
so the GUI and any future front-end never drift on what "85% is red" means.
"""
from __future__ import annotations

from datetime import datetime, timezone

from usage_dashboard.shared.models import Reading, ReadingStatus

# Colour palette (RGB), matching the web dashboard's thresholds.
BG = (0, 0, 0)
TEXT = (255, 255, 255)
GRAY = (150, 150, 150)
GREEN = (0x22, 0xC5, 0x5E)
ORANGE = (0xF9, 0x73, 0x16)
RED = (0xEF, 0x44, 0x44)
YELLOW = (0xEA, 0xB3, 0x08)
# Indigo matching umans' own low-interactivity banner, so the footer reads as
# the same signal the provider's UI shows.
BLUE = (0x81, 0x8C, 0xF8)
BAR_BG = (50, 50, 50)

_THREE_DAYS_SECONDS = 3 * 24 * 3600

# Percentage thresholds for the bar colour.
WARN_PERCENT = 75.0
CRIT_PERCENT = 85.0


def mute(color: tuple[int, int, int], amount: float = 0.55) -> tuple[int, int, int]:
    """Blend *color* toward the bar-track gray so a second account's bars read as
    a quieter, secondary set while keeping the green/orange/red hue legible.
    *amount* is how far to blend (0 = unchanged, 1 = fully gray)."""
    amount = max(0.0, min(1.0, amount))
    return tuple(  # type: ignore[return-value]
        round(c + (g - c) * amount) for c, g in zip(color, BAR_BG)
    )


def bar_color(percent: float | None) -> tuple[int, int, int]:
    """Green/orange/red by utilization, gray when unknown."""
    if percent is None:
        return GRAY
    if percent >= CRIT_PERCENT:
        return RED
    if percent >= WARN_PERCENT:
        return ORANGE
    return GREEN


def format_countdown(
    resets_at: datetime | None, now: datetime | None = None
) -> tuple[str, bool]:
    """Return ``(text, highlight)`` for a reset time.

    ``highlight`` is True when the reset is within three days (worth drawing
    attention to). *now* is injectable for tests.
    """
    if resets_at is None:
        return ("", False)
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if resets_at.tzinfo is None:
        target = resets_at.replace(tzinfo=timezone.utc)
    else:
        target = resets_at.astimezone(timezone.utc)
    total_seconds = int((target - now).total_seconds())
    if total_seconds <= 0:
        return ("0m", True)
    within_threshold = total_seconds <= _THREE_DAYS_SECONDS
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    if days > 0:
        return (f"{days}d {hours}h", within_threshold)
    return (f"{hours}h {minutes}m", within_threshold)


def status_suffix(reading: Reading) -> str:
    """The ``[stale]`` / ``[offline]`` tag appended to a provider title."""
    if reading.status is ReadingStatus.OFFLINE:
        return " [offline]"
    if reading.status is ReadingStatus.STALE or reading.stale:
        return " [stale]"
    return ""


def percent_text(percent: float | None) -> str:
    return f"{percent:.0f}%" if percent is not None else "N/A"


def to_local(utc_naive: datetime) -> datetime:
    """Convert a naive-UTC datetime to the system's local timezone."""
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    return utc_naive.replace(tzinfo=timezone.utc).astimezone(local_tz)


def format_interval(seconds: int) -> str:
    """Compact interval label: 60 -> '1m', 300 -> '5m', 30 -> '30s'."""
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"
