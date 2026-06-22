"""Backlight sleep schedule — pure time logic, no I/O.

Like :mod:`layout` and :mod:`format`, this module is deliberately side-effect
free so the scheduling rules are unit-tested in isolation; the pygame loop
(:mod:`gui`) and the backlight sysfs writer (:mod:`backlight`) are the thin
layers that act on its decisions.

A schedule is a set of weekly-recurring sleep windows. Times are local wall-clock
(the user's clock), so callers pass naive local ``datetime`` values.

Tap-to-wake rule (see plans/002): a tap during sleep keeps the panel awake until
the *earlier of* (a) the current contiguous sleep period's natural end, or
(b) the next local midnight — then the schedule takes over again.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

_DAY = 24 * 60
_WEEK = 7 * _DAY  # minutes in a week


def _weekmin(dt: datetime) -> int:
    """Minutes since Monday 00:00 (0..10079); seconds truncated."""
    return dt.weekday() * _DAY + dt.hour * 60 + dt.minute


def _next_midnight(dt: datetime) -> datetime:
    """The first local 00:00 strictly after *dt*."""
    return datetime.combine(dt.date() + timedelta(days=1), time(0, 0))


@dataclass(frozen=True)
class SleepWindow:
    """A weekly-recurring sleep window, in minutes-since-Monday-00:00.

    ``end`` may be <= ``start`` to denote a window that wraps across the week
    boundary (e.g. Fri 18:00 -> Mon 08:00); the covered length is always
    ``(end - start) % _WEEK``.
    """

    start: int
    end: int


def _at(day: int, hour: int, minute: int = 0) -> int:
    """Week-minute for weekday *day* (0=Mon) at *hour*:*minute*."""
    return day * _DAY + hour * 60 + minute


_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# The fleet default, as a spec string (the same format the server delivers and
# the BACKLIGHT_SCHEDULE env override accepts). Nightly 00:00-08:00 plus a
# weekend block Fri 18:00 -> Mon 08:00.
DEFAULT_SCHEDULE_SPEC = "daily 00:00-08:00; fri 18:00-mon 08:00"


def _parse_hhmm(text: str) -> int:
    """``HH:MM`` -> minutes past midnight (0..1439)."""
    hh, mm = text.strip().split(":")
    h, m = int(hh), int(mm)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"bad time {text!r}")
    return h * 60 + m


def _parse_rule(rule: str) -> list[SleepWindow]:
    """One ``;``-separated rule -> its sleep window(s).

    Two forms:
      ``daily HH:MM-HH:MM``           applied to all seven days
      ``<day> HH:MM-<day> HH:MM``     a single span (may cross days/the week)
    A daily window whose end <= start crosses midnight into the next day.
    """
    body = rule.strip()
    lower = body.lower()
    if lower.startswith("daily "):
        start_s, end_s = body[len("daily "):].split("-")
        s, e = _parse_hhmm(start_s), _parse_hhmm(end_s)
        out: list[SleepWindow] = []
        for d in range(7):
            start = d * _DAY + s
            end = d * _DAY + e + (_DAY if e <= s else 0)  # cross-midnight -> next day
            out.append(SleepWindow(start, end))
        return out
    left, right = body.split("-")
    sd, st = left.split()
    ed, et = right.split()
    start = _DAYS[sd.lower()] * _DAY + _parse_hhmm(st)
    end = _DAYS[ed.lower()] * _DAY + _parse_hhmm(et)
    return [SleepWindow(start, end)]


def parse_schedule(spec: str) -> "SleepSchedule":
    """Build a :class:`SleepSchedule` from a spec string. Raises ``ValueError``
    on a malformed spec (so a bad remote/env value can be rejected and the
    caller fall back to a known-good schedule)."""
    windows: list[SleepWindow] = []
    for raw in spec.split(";"):
        if raw.strip():
            try:
                windows.extend(_parse_rule(raw))
            except (ValueError, KeyError) as exc:
                raise ValueError(f"bad schedule rule {raw.strip()!r}: {exc}") from exc
    return SleepSchedule(windows)


def default_sleep_schedule() -> "SleepSchedule":
    """The fleet default (plans/002), parsed from ``DEFAULT_SCHEDULE_SPEC``."""
    return parse_schedule(DEFAULT_SCHEDULE_SPEC)


class SleepSchedule:
    """Decides asleep/awake and the tap-to-wake deadline for a set of windows.

    Internally the weekly windows are expanded across adjacent weeks and merged
    into maximal contiguous sleep intervals on an absolute minute line, so a
    query near a week boundary (and the nightly/weekend overlap) resolves
    correctly.
    """

    # Lay windows down over these week offsets, then query in the middle copy,
    # so a wrap in either direction is covered by a fully-formed neighbour.
    _OFFSETS = (-_WEEK, 0, _WEEK, 2 * _WEEK)

    def __init__(self, windows: list[SleepWindow]) -> None:
        self._windows = windows
        self._merged = self._build_merged(windows)

    @classmethod
    def _build_merged(cls, windows: list[SleepWindow]) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        for w in windows:
            length = (w.end - w.start) % _WEEK
            if length == 0:
                continue
            for off in cls._OFFSETS:
                spans.append((w.start + off, w.start + off + length))
        spans.sort()
        merged: list[tuple[int, int]] = []
        for s, e in spans:
            if merged and s <= merged[-1][1]:  # overlap or touch -> extend
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        return merged

    def _containing_span(self, dt: datetime) -> tuple[int, int] | None:
        """The merged absolute-minute span containing *dt*, or None if awake."""
        p = _WEEK + _weekmin(dt)  # query in the middle copy
        for s, e in self._merged:
            if s <= p < e:
                return s, e
        return None

    def is_asleep(self, dt: datetime) -> bool:
        return self._containing_span(dt) is not None

    def current_window_end(self, dt: datetime) -> datetime | None:
        """When the contiguous sleep period covering *dt* ends, or None if awake.

        Truncated to the minute (seconds dropped), consistent with the rest of
        the module.
        """
        span = self._containing_span(dt)
        if span is None:
            return None
        p = _WEEK + _weekmin(dt)
        minute_truncated = dt.replace(second=0, microsecond=0)
        return minute_truncated + timedelta(minutes=span[1] - p)

    def wake_until(self, dt: datetime) -> datetime | None:
        """Deadline for a tap-to-wake at *dt*: the earlier of the current sleep
        window's end and the next local midnight. None if not currently asleep.
        """
        end = self.current_window_end(dt)
        if end is None:
            return None
        return min(end, _next_midnight(dt))
