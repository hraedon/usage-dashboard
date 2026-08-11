"""Display formatting shared by the Pi client and the web dashboard.

The two surfaces are separate renderers over the same model, so anything that
decides *what text a user reads* belongs here rather than in either one. This
module exists because ``client/format.format_duration`` and
``server/api._countdown_short`` had drifted into byte-identical copies of each
other — harmless while they agreed, but it is the same duplication that let the
z.ai peak countdown ship on the panel and never reach the web view (WI-030,
and WI-020 before it).
"""
from __future__ import annotations


def format_duration(total_seconds: float) -> str:
    """Compact duration label: 45 -> '1m', 12240 -> '3h 24m', 176400 -> '2d 1h'.

    Sub-minute durations round up to '1m' rather than showing '0m', so a
    countdown never reads as already-expired while the window is still open.
    """
    seconds = max(0, int(total_seconds))
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{max(1, minutes)}m"
