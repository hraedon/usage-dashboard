"""Brightness step model and best-effort persistence.

Pure helpers (no pygame, no hardware) that map a ``+``/``−`` step index onto a
panel ``brightness`` level and back, so the GUI's brightness overlay is
unit-tested without a display. The *number of steps* is configurable
(``BRIGHTNESS_STEPS``) so the granularity of a nudge can be tuned per unit
without code changes.

Persistence stores the raw brightness *level* (not the step) so it is
independent of the step count — re-tuning ``BRIGHTNESS_STEPS`` re-buckets the
same physical brightness rather than meaning something different. Both load and
save are best-effort: a missing/unreadable/unwritable file degrades to "no
persisted level", never an exception, so a read-only home or a fresh unit just
falls back to the hardware's current brightness.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def level_for_step(step: int, steps: int, max_level: int) -> int:
    """Brightness level for *step* (1..*steps*) on a ``[1, max_level]`` scale.

    Step 1 maps to the dimmest *visible* level (never 0 — that's sleep) and
    *steps* maps to ``max_level``."""
    if steps < 1 or max_level < 1:
        return max(1, max_level)
    step = max(1, min(steps, step))
    return max(1, round(step / steps * max_level))


def step_for_level(level: int, steps: int, max_level: int) -> int:
    """The step (1..*steps*) whose brightness is nearest *level*. Inverse of
    :func:`level_for_step`, clamped into range."""
    if steps < 1 or max_level < 1:
        return 1
    level = max(0, min(max_level, level))
    return max(1, min(steps, round(level / max_level * steps)))


def load_level(path: Path) -> int | None:
    """The persisted brightness level, or None if absent/unreadable/garbage."""
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def save_level(path: Path, level: int) -> None:
    """Persist *level* best-effort, creating parent dirs; swallow write errors."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(int(level)))
    except OSError as exc:
        logger.warning("brightness persist failed (%s): %s", path, exc)
