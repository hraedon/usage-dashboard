"""Backlight power control via the Linux sysfs backlight class.

Thin, defensive wrapper: it auto-discovers the panel's backlight device under
``/sys/class/backlight`` and toggles ``bl_power``. Everything no-ops safely when
no device is present (dev machine, windowed mode) or the node isn't writable, so
the GUI can call it unconditionally.

``bl_power`` follows the kernel FB_BLANK convention: ``0`` unblanks (backlight
on), any non-zero value blanks it (off). We write ``0``/``1``.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SYSFS_BASE = Path("/sys/class/backlight")
_ON = "0"   # FB_BLANK_UNBLANK
_OFF = "1"  # FB_BLANK_NORMAL (any non-zero blanks)


class Backlight:
    """Controls panel backlight power, no-op when unavailable."""

    def __init__(self, base: Path | None = None) -> None:
        self._path = self._discover(base if base is not None else _SYSFS_BASE)
        self._last: str | None = None
        if self._path is None:
            logger.info("no backlight device found; backlight control disabled")
        else:
            logger.info("backlight control via %s", self._path)

    @staticmethod
    def _discover(base: Path) -> Path | None:
        """First ``<device>/bl_power`` node under *base*, or None."""
        try:
            if not base.is_dir():
                return None
            for device in sorted(base.iterdir()):
                node = device / "bl_power"
                if node.exists():
                    return node
        except OSError as exc:
            logger.warning("backlight discovery failed under %s: %s", base, exc)
        return None

    @property
    def available(self) -> bool:
        return self._path is not None

    def set_power(self, on: bool) -> None:
        """Turn the backlight on/off. Skips redundant writes; logs and swallows
        write errors (e.g. permissions) rather than crashing the GUI loop."""
        if self._path is None:
            return
        value = _ON if on else _OFF
        if value == self._last:
            return
        try:
            self._path.write_text(value)
            self._last = value
            logger.info("backlight %s", "on" if on else "off")
        except OSError as exc:
            logger.warning("backlight write to %s failed: %s", self._path, exc)
