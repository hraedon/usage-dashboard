"""Backlight power control via the Linux sysfs backlight class.

Thin, defensive wrapper: it auto-discovers the panel's backlight device under
``/sys/class/backlight`` and turns it off/on by writing ``brightness``.
Everything no-ops safely when no device is present (dev machine, windowed mode)
or the node isn't writable, so the GUI can call it unconditionally.

We drive ``brightness`` (0 = backlight fully off on the Touch Display 2, verified
on the units) rather than ``bl_power`` because ``brightness`` is writable by the
``video`` group the GUI already runs in, whereas ``bl_power`` is root-only — so
this needs no udev rule or privileged helper. Waking restores the brightness the
panel had at startup.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SYSFS_BASE = Path("/sys/class/backlight")
_OFF = 0


class Backlight:
    """Controls panel backlight power via ``brightness``; no-op when unavailable."""

    def __init__(self, base: Path | None = None) -> None:
        self._device = self._discover(base if base is not None else _SYSFS_BASE)
        self._on_level = self._read_on_level()
        self._last_on: bool | None = None
        if self._device is None:
            logger.info("no backlight device found; backlight control disabled")
        else:
            logger.info(
                "backlight control via %s (wake level %d)",
                self._device / "brightness", self._on_level,
            )

    @staticmethod
    def _discover(base: Path) -> Path | None:
        """First device dir under *base* exposing a ``brightness`` node, or None."""
        try:
            if not base.is_dir():
                return None
            for device in sorted(base.iterdir()):
                if (device / "brightness").exists():
                    return device
        except OSError as exc:
            logger.warning("backlight discovery failed under %s: %s", base, exc)
        return None

    @staticmethod
    def _read_int(path: Path, default: int) -> int:
        try:
            return int(path.read_text().strip())
        except (OSError, ValueError):
            return default

    def _read_on_level(self) -> int:
        """Brightness to restore on wake: the level present at startup, or
        max_brightness if we started up dark (never restore to off)."""
        if self._device is None:
            return 0
        current = self._read_int(self._device / "brightness", default=0)
        if current > 0:
            return current
        max_b = self._read_int(self._device / "max_brightness", default=0)
        return max_b if max_b > 0 else 1

    @property
    def available(self) -> bool:
        return self._device is not None

    def set_power(self, on: bool) -> None:
        """Turn the backlight on (restore brightness) or off (brightness 0).
        Skips redundant writes; logs and swallows write errors rather than
        crashing the GUI loop."""
        if self._device is None or on == self._last_on:
            return
        target = self._on_level if on else _OFF
        try:
            (self._device / "brightness").write_text(str(target))
            self._last_on = on
            logger.info("backlight %s (brightness=%d)", "on" if on else "off", target)
        except OSError as exc:
            logger.warning("backlight write failed (%s): %s", self._device, exc)
