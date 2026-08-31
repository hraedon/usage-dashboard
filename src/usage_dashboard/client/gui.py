"""Fullscreen pygame touch GUI for the Raspberry Pi 4B + 5" touch display.

The drawable model and all geometry/colour decisions live in :mod:`layout` and
:mod:`format` (unit-tested). This module is the thin pygame layer: it owns the
window, the event loop, font sizing, and blitting — plus touch routing through
``layout.tap_transition``.

Run on the Pi via the ``usage-dashboard-gui`` entry point. Requires the ``gui``
extra (``pip install 'usage-dashboard[gui]'``).
"""
from __future__ import annotations

import logging
import math
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pygame

from usage_dashboard.client import diagnostics as diag
from usage_dashboard.client import format as fmt
from usage_dashboard.client.backlight import Backlight
from usage_dashboard.client.brightness import (
    level_for_step,
    load_level,
    save_level,
    step_for_level,
)
from usage_dashboard.client.fetcher import ClientFetcher
from usage_dashboard.client.layout import (
    BarSpec,
    BrightnessOverlay,
    Color,
    DetailLayout,
    MainLayout,
    Rect,
    StatusOverlay,
    TileSpec,
    ViewState,
    build_detail_layout,
    build_main_layout,
    build_status_overlay,
    refresh_hit_test,
    rotate_touch_norm,
    status_overlay_footer_height,
    status_overlay_padding,
    tap_transition,
)
from usage_dashboard.client.schedule import ScheduleResolver, SleepSchedule
from usage_dashboard.shared.models import Provider, Reading

logger = logging.getLogger(__name__)

_TILE_BG = (17, 17, 17)
_OVERLAY_BG = (28, 28, 28)
_BTN_BG = (45, 45, 45)
# A refresh POST is in flight: fill the button so the user sees the tap landed.
_REFRESH_PENDING_BG = (92, 72, 10)
# How long the refresh result ("refreshed"/"refresh failed") stays on screen.
_REFRESH_FEEDBACK_MS = 6000
_MIN_BAR_TRACK_W = 24

# Nominal pygame font sizes are not glyph sizes: the default font renders an
# 18px request with a 12px line height.  These are minimum *rendered* line
# heights for the three text tiers on the smallest audited display.
_MIN_BODY_GLYPH_H = 16
_MIN_SMALL_GLYPH_H = 14
_MIN_TITLE_GLYPH_H = 20


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


class DoubleTapDetector:
    """Detects a double-tap: two taps within ``window_ms`` and ``tolerance_px``
    of each other.

    Pure logic — the caller supplies a monotonic millisecond clock and the tap
    position — so the gesture timing is unit-tested without pygame. The position
    tolerance is what keeps a quick *open-tile-then-tap-back* (two taps in
    different places) from registering as the deliberate same-spot double-tap.
    """

    def __init__(self, window_ms: int = 350, tolerance_px: int = 80) -> None:
        self._window_ms = window_ms
        self._tolerance_px = tolerance_px
        self._last_ms: int | None = None
        self._last_pos: tuple[int, int] | None = None

    def reset(self) -> None:
        self._last_ms = None
        self._last_pos = None

    def register(self, now_ms: int, pos: tuple[int, int]) -> bool:
        """Record a tap; return True if it completes a double-tap. On a match the
        state resets, so a third quick tap starts a fresh pair rather than
        chaining into overlapping triple-taps."""
        prev_ms, prev_pos = self._last_ms, self._last_pos
        self._last_ms, self._last_pos = now_ms, pos
        if prev_ms is None or prev_pos is None:
            return False
        if not (0 <= now_ms - prev_ms <= self._window_ms):
            return False
        dx, dy = pos[0] - prev_pos[0], pos[1] - prev_pos[1]
        if dx * dx + dy * dy > self._tolerance_px * self._tolerance_px:
            return False
        self.reset()  # consume the pair
        return True


class DashboardGui:
    """Owns the pygame window, fonts, view state, and render loop."""

    @staticmethod
    def _readable_font(nominal_size: int, min_height: int) -> Any:
        """Create a font whose actual line and ``Ag`` glyph heights meet *min_height*.

        ``pygame.font.Font`` accepts a nominal point size, not a guaranteed
        pixel height.  Measuring the resulting font is important on the small
        fallback display (and keeps this contract stable if the bundled font
        changes between pygame builds).
        """
        size = max(1, nominal_size)
        font = pygame.font.Font(None, size)
        while (
            font.get_height() < min_height
            or font.render("Ag", True, (255, 255, 255)).get_bounding_rect().height
            < min_height
        ):
            size += 1
            font = pygame.font.Font(None, size)
        return font

    def __init__(
        self,
        fetcher: ClientFetcher,
        size: tuple[int, int],
        fps: int = 10,
        touch_rotate: int = 0,
        schedule_resolver: ScheduleResolver | None = None,
        backlight: Backlight | None = None,
        brightness_steps: int = 10,
        brightness_state_file: Path | None = None,
        server_url: str = "",
        status_dir: Path | None = None,
    ) -> None:
        self._fetcher = fetcher
        screen = pygame.display.get_surface()
        if screen is None:
            raise RuntimeError("no pygame display surface; call set_mode() first")
        self._screen = screen
        self._width, self._height = size
        self._fps = fps
        # Clockwise display rotation (matches cmdline.txt rotate=N) so touch
        # coordinates land on the rotated framebuffer. 0 on a dev window.
        self._touch_rotate = touch_rotate % 360
        self._clock = pygame.time.Clock()
        self._state = ViewState()
        self._running = True
        # Backlight sleep. _resolver None = never sleep. When set, each loop
        # resolves the active schedule (server spec > env > default) so a remote
        # change takes effect without a restart. While dark the loop keeps
        # running to catch the wake tap but skips drawing; a tap sets _wake_until
        # (the schedule's deadline) and wakes the panel.
        self._resolver = schedule_resolver
        self._schedule: SleepSchedule | None = None
        self._backlight = backlight if backlight is not None else Backlight()
        self._wake_until: datetime | None = None
        self._dark = False
        # Manual sleep: a double-tap blanks the panel immediately, independent of
        # the schedule, until the next tap wakes it. Sticky across schedule
        # changes; only a wake tap clears it. The position tolerance scales with
        # the panel so a same-spot double-tap is forgiving on any resolution.
        self._manual_sleep = False
        self._double_tap = DoubleTapDetector(
            tolerance_px=max(40, min(self._width, self._height) // 6)
        )
        # Status overlay: tapping the status ("Updated…") line opens a card with
        # unit diagnostics (hostname/IPs, server, running commit, updater health)
        # on the left and brightness +/- on the right. The step count (nudge
        # granularity) is configurable so a unit can try 9/10/11/… without code
        # changes; the chosen *level* is persisted best-effort so it survives a
        # reboot, and applied to the panel at startup. Diagnostics are gathered
        # once when the overlay opens (cheap, but no need to re-poll per frame).
        self._brightness_steps = max(2, brightness_steps)
        self._brightness_state_file = brightness_state_file
        self._brightness_step = self._init_brightness_step()
        self._server_url = server_url
        self._status_dir = status_dir
        self._diag: diag.Diagnostics | None = None
        # On-demand refresh (WI-012): a tap on the status-bar refresh button
        # POSTs /refresh in a background thread; the button stays highlighted
        # while it's in flight so the tap reads as acknowledged.
        self._refresh_pending = False
        self._refresh_feedback = ""
        # Wall-clock deadline (pygame ticks, ms) after which the feedback text
        # clears itself, so "refreshed"/"refresh failed" doesn't sit there for
        # the rest of the day.
        self._refresh_feedback_until = 0
        self._refresh_thread: threading.Thread | None = None
        # While dark we tick slowly to save CPU but still pump touch events.
        self._sleep_fps = 4
        # Fonts scale from the short edge.  Height-only scaling made the
        # portrait 720x1280 framebuffer select an 85px unit and left its
        # half-width tiles with no room for labels or tracks.  The short edge
        # keeps the established 48px unit at both 1280x720 and 720x1280 while
        # still giving the 240x320 fallback a readable rendered body glyph.
        unit = max(24, min(self._width, self._height) // 15)
        self._font = self._readable_font(unit, _MIN_BODY_GLYPH_H)
        self._font_small = self._readable_font(
            max(22, unit * 4 // 5), _MIN_SMALL_GLYPH_H
        )
        self._font_title = self._readable_font(
            max(32, unit * 5 // 4), _MIN_TITLE_GLYPH_H
        )
        # Oversized glyphs for the brightness overlay's +/- buttons and readout.
        self._font_big = self._readable_font(unit * 2, 28)
        # Below this width the layout stacks the paired providers.  Full-width
        # rows let the narrow fallback keep the reset countdown visible instead
        # of squeezing it into a zero-width track.
        self._narrow = self._width < 480
        # Fixed width reserved on the right of every bar row for the reset text,
        # sized to a worst-case countdown so the bar track always ends at the
        # same x and never bleeds into the countdown. Full-width tiles show
        # "resets 23d 23h"; compact (paired) tiles show a bare "23d 23h", so
        # their column is narrower and the bar track extends further.
        # Tile padding derived from the screen, not per-tile height, so a
        # 3-bar tile gets the same padding as a 2-bar tile — the taller row
        # accommodates the extra bar, not extra padding.
        self._tile_pad = max(8, min(self._width, self._height) // 40)
        # Exact per-tile overhead (title + padding) passed to the layout so
        # row heights distribute with the real font height, not an estimate.
        # This is what makes 2-bar and 3-bar tiles get equal row heights and
        # equal bottom padding.
        self._tile_overhead = (
            self._tile_pad
            + self._font_title.get_linesize()
            + self._tile_pad // 2
            + self._tile_pad
        )

    # -- event loop ---------------------------------------------------------

    def stop(self) -> None:
        self._running = False

    # -- brightness ---------------------------------------------------------

    def _init_brightness_step(self) -> int:
        """Resolve the starting step: a persisted level (re-applied to the panel
        so it survives a reboot) if present, else the panel's current
        brightness. Falls back to full when there's no controllable backlight."""
        if not self._backlight.available:
            return self._brightness_steps
        max_level = self._backlight.max_level
        persisted = (
            load_level(self._brightness_state_file)
            if self._brightness_state_file is not None else None
        )
        if persisted is not None:
            self._backlight.set_level(persisted)
            return step_for_level(persisted, self._brightness_steps, max_level)
        return step_for_level(
            self._backlight.current_level, self._brightness_steps, max_level
        )

    def _nudge_brightness(self, delta: int) -> None:
        """Move the brightness one step (clamped to the rails), push it to the
        panel, and persist the chosen level. No-op at a rail or with no
        backlight."""
        if not self._backlight.available:
            return
        step = max(1, min(self._brightness_steps, self._brightness_step + delta))
        if step == self._brightness_step:
            return
        self._brightness_step = step
        level = level_for_step(step, self._brightness_steps, self._backlight.max_level)
        self._backlight.set_level(level)
        if self._brightness_state_file is not None:
            save_level(self._brightness_state_file, level)

    # -- on-demand refresh (WI-012) ------------------------------------------

    def _start_refresh(self) -> None:
        """POST /refresh and show feedback while the server collects.

        Runs in a background thread so the draw loop isn't blocked for the
        (up to tens of seconds) it can take to fetch every provider; the
        button stays highlighted via ``_refresh_pending`` until it returns.
        """
        if self._refresh_pending:
            return  # a refresh is already in flight
        self._refresh_pending = True
        self._set_refresh_feedback("refreshing\u2026", hold_ms=_REFRESH_FEEDBACK_MS)
        self._refresh_thread = threading.Thread(target=self._run_refresh, daemon=True)
        self._refresh_thread.start()

    def _set_refresh_feedback(self, text: str, hold_ms: int) -> None:
        self._refresh_feedback = text
        self._refresh_feedback_until = pygame.time.get_ticks() + hold_ms

    def _current_refresh_feedback(self) -> str:
        """The feedback text if it hasn't aged out, else "" (drops it)."""
        if self._refresh_feedback and (
            self._refresh_pending
            or pygame.time.get_ticks() < self._refresh_feedback_until
        ):
            return self._refresh_feedback
        self._refresh_feedback = ""
        return ""

    def _run_refresh(self) -> None:
        try:
            request = getattr(self._fetcher, "request_refresh", None)
            ok = request() if callable(request) else False
            text = "refreshed" if ok else "refresh failed"
        except Exception as exc:  # never let a refresh crash the thread
            logger.warning("Refresh failed: %s", exc)
            text = "refresh failed"
        finally:
            self._refresh_pending = False
        # Set after clearing _refresh_pending so the hold window starts when the
        # result actually lands, not while the request is still in flight.
        self._set_refresh_feedback(text, hold_ms=_REFRESH_FEEDBACK_MS)

    def _handle_overlay_tap(self, pos: tuple[int, int]) -> None:
        """Route a tap while the status overlay is open: the right-column
        ``−``/``+`` nudge brightness; a tap outside the card closes it; taps on
        the diagnostics column or the readout do nothing."""
        overlay = build_status_overlay((self._width, self._height))
        if overlay.brightness.minus.contains(*pos):
            self._nudge_brightness(-1)
        elif overlay.brightness.plus.contains(*pos):
            self._nudge_brightness(+1)
        elif not overlay.panel.contains(*pos):
            self._state = ViewState()

    def _tap_position(self, event: pygame.event.Event) -> tuple[int, int] | None:
        if event.type == pygame.MOUSEBUTTONDOWN:
            # Real mouse (dev/windowed mode): already in screen pixels.
            return int(event.pos[0]), int(event.pos[1])
        if event.type == pygame.FINGERDOWN:
            # Touch coords are normalised 0..1 in the panel's native frame;
            # rotate them onto the (possibly rotated) framebuffer.
            nx, ny = rotate_touch_norm(event.x, event.y, self._touch_rotate)
            return int(nx * self._width), int(ny * self._height)
        return None

    def _handle_events(
        self, layout: MainLayout, swallow_wake: bool = False,
        now: datetime | None = None,
    ) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self._running = False
            elif event.type == pygame.KEYDOWN and event.key in (
                pygame.K_ESCAPE, pygame.K_q
            ):
                self._running = False
            else:
                pos = self._tap_position(event)
                if pos is None:
                    continue
                if swallow_wake:
                    # Panel is dark: a tap only wakes it; it is NOT routed into a
                    # tile. Clears a manual (double-tap) sleep, and — if a
                    # schedule window also has it asleep — holds it awake until
                    # that window's deadline.
                    self._manual_sleep = False
                    self._double_tap.reset()
                    if self._schedule is not None and now is not None:
                        self._wake_until = self._schedule.wake_until(now)
                    continue
                if self._state.overlay:
                    # Overlay open: route to its buttons; never feed the
                    # double-tap detector so rapid +/- taps don't sleep the panel.
                    self._handle_overlay_tap(pos)
                    continue
                if self._double_tap.register(pygame.time.get_ticks(), pos):
                    # A double-tap puts the panel to sleep now (if the backlight
                    # is actually controllable) and drops back to the home grid,
                    # so it wakes on the dashboard rather than a stale detail
                    # view. Swallow this second tap either way.
                    if self._backlight.available:
                        self._manual_sleep = True
                        self._state = ViewState()
                    continue
                if (
                    self._state.detail_provider is None
                    and refresh_hit_test(layout, pos)
                ):
                    # On-demand refresh button (WI-012): POST /refresh and show
                    # feedback while the server collects fresh readings.
                    self._start_refresh()
                    continue
                if (
                    self._state.detail_provider is None
                    and layout.status_rect.contains(*pos)
                ):
                    # Single tap on the "Updated…" line opens the status overlay.
                    # Always available — diagnostics are useful even with no
                    # controllable backlight (the +/- side then just no-ops).
                    # Gather diagnostics now; reset the double-tap pair so the
                    # opening tap can't later combine with a tap in the overlay.
                    self._diag = diag.gather(self._status_dir, self._server_url)
                    self._double_tap.reset()
                    self._state = ViewState(overlay=True)
                    continue
                self._state = tap_transition(self._state, layout, pos)

    def _is_dark(self, now: datetime) -> bool:
        """True if the panel should be blanked now (asleep and not tap-woken)."""
        if self._manual_sleep:  # double-tap override, independent of the schedule
            return True
        if self._schedule is None:
            return False
        if self._wake_until is not None and now < self._wake_until:
            return False
        return self._schedule.is_asleep(now)

    def run(self) -> None:
        while self._running:
            now = datetime.now()  # local wall clock for the sleep schedule
            # Resolve the active schedule each frame so a remote schedule change
            # (picked up by the fetcher) takes effect without a restart.
            self._schedule = (
                self._resolver.resolve(self._fetcher.current_schedule_spec)
                if self._resolver is not None
                else None
            )
            if self._wake_until is not None and now >= self._wake_until:
                self._wake_until = None
            dark = self._is_dark(now)
            self._dark = dark
            readings = self._fetcher.get_latest_readings()
            layout = build_main_layout(
                readings, (self._width, self._height),
                refresh_interval=self._fetcher.current_interval,
                tile_overhead=self._tile_overhead,
                refresh_pending=self._refresh_pending,
            )
            # While dark, a tap wakes (sets _wake_until) instead of navigating.
            self._handle_events(layout, swallow_wake=dark, now=now)
            dark = self._is_dark(now)  # a wake tap may have just cleared it
            self._backlight.set_power(on=not dark)
            if not dark:
                self._screen.fill(fmt.BG)
                if self._state.overlay:
                    # The grid stays behind for context; the card sits on top.
                    self._draw_main(layout)
                    self._draw_status_overlay()
                elif self._state.detail_provider is None:
                    self._draw_main(layout)
                else:
                    self._draw_detail(readings)
                pygame.display.flip()
            self._clock.tick(self._fps if not dark else self._sleep_fps)

    # -- rendering ----------------------------------------------------------

    @staticmethod
    def _fit_text(font: Any, text: str, max_width: int) -> str:
        """Return *text* shortened to a pixel width without clipping it.

        Pygame blits happily clip a surface at the edge of the display, which
        is particularly easy to miss on the small fallback display.  All text
        that has a bounded column goes through this helper.  ASCII dots are
        used for the fallback marker because the bundled pygame font is not
        guaranteed to contain every Unicode ellipsis glyph.
        """
        if max_width <= 0:
            return ""
        if font.size(text)[0] <= max_width:
            return text
        marker = "..."
        marker_width = font.size(marker)[0]
        if marker_width > max_width:
            # Keep as much of the text as can be drawn when even the marker is
            # wider than the column.  The loop is tiny (labels are short) and
            # avoids relying on font metrics being monotonic across glyphs.
            for end in range(len(text), 0, -1):
                candidate = text[:end]
                if font.size(candidate)[0] <= max_width:
                    return candidate
            return ""
        for end in range(len(text), 0, -1):
            candidate = text[:end].rstrip() + marker
            if font.size(candidate)[0] <= max_width:
                return candidate
        return marker if marker_width <= max_width else ""

    def _bar_label(self, bar: BarSpec) -> str:
        """The left-column text for a bar: ``[account · ]Label NN%``."""
        return (
            f"{(bar.account + ' · ') if bar.account else ''}"
            f"{bar.label} {bar.percent_text}"
        )

    def _bar_font(self, compact: bool = False) -> Any:
        """Font for bar rows, shrinking compact columns on portrait panels.

        The title remains the large glanceable font.  A paired 339px tile on a
        720px portrait screen, however, cannot spend the same 48px unit on both
        its labels and reset countdown that a 601px landscape tile can.  The
        small font is still intentionally larger than a desktop body font and
        is only used for genuinely constrained compact rows.
        """
        if compact and self._width < 900:
            return self._font_small
        return self._font

    def _reset_width(self, compact: bool = False) -> int:
        """Width reserved for a reset countdown in the requested mode.

        On a genuinely narrow display the ``resets`` prefix costs more than it
        communicates.  Dropping that prefix for full-width fallback rows gives
        the track a useful minimum width while retaining the countdown itself.
        """
        font = self._bar_font(compact)
        text = "23d 23h" if compact or self._narrow else "resets 23d 23h"
        return int(font.size(text)[0])

    def _reset_label(self, bar: BarSpec, compact: bool = False) -> str:
        text = bar.reset_text if compact or self._narrow else f"resets {bar.reset_text}"
        return text

    def _label_col_widths(self, tiles: list[TileSpec]) -> dict[bool, int]:
        """Widest bar label per tile group (compact vs full-width).

        Full-width tiles (Session/Weekly) and compact tiles (S/W) are sized
        independently so each group's bars start at the same x without the
        wide labels stealing space from the narrow paired tiles.  The natural
        width is capped by the narrowest tile in each group, leaving room for a
        reset column and a drawable track."""
        widths: dict[bool, int] = {False: 0, True: 0}
        for tile in tiles:
            bar_font = self._bar_font(tile.compact)
            for bar in tile.bars:
                w = bar_font.size(self._bar_label(bar))[0]
                if w > widths[tile.compact]:
                    widths[tile.compact] = w
        for compact in (False, True):
            group = [tile for tile in tiles if tile.compact == compact and tile.bars]
            if not group or widths[compact] == 0:
                continue
            reset_w = self._reset_width(compact)
            max_width = min(
                tile.rect.w - 4 * self._tile_pad - reset_w - _MIN_BAR_TRACK_W
                for tile in group
            )
            widths[compact] = min(widths[compact], max(0, max_width))
        return widths

    def _bar_track(self, rect: Rect, label_col_w: int, compact: bool = False) -> tuple[int, int]:
        """``(track_x, track_right)`` for a tile's bars given the label column
        width. Compact tiles use a narrower reset column (bare countdown, no
        "resets" prefix) so the bar track extends further."""
        pad = self._tile_pad
        reset_w = self._reset_width(compact)
        track_x = rect.x + pad + label_col_w + pad
        track_right = rect.x + rect.w - pad - reset_w - pad
        return track_x, track_right

    @staticmethod
    def _ultra_compact_bar_text(bar: BarSpec) -> str:
        """Return the smallest useful label for a summary percentage."""
        label = bar.label.upper()
        if label.startswith("SESSION"):
            label = "S"
        elif label.startswith("WEEKLY"):
            label = "W"
        else:
            label = label[:1]
        labelled = f"{label}{bar.percent_text}"
        return labelled

    def _ultra_compact_text_specs(
        self, tile: TileSpec,
    ) -> list[tuple[str, Any, str, Color, Rect]]:
        """Text surfaces and rectangles for a tiny summary tile.

        This is intentionally the single geometry source for both drawing and
        regression tests. The first two percentage windows remain visible in
        the summary; reset countdowns, subtitles, and extra scoped/account
        windows are detail-view information on a frame this small.
        """
        r = tile.rect
        pad = self._tile_pad
        left = r.x + pad
        right = r.x + r.w - pad
        top = r.y + pad
        inner_w = max(0, right - left)
        title_font = self._font_title
        marker_font = title_font
        marker = (
            marker_font.render(tile.status_marker, True, tile.status_marker_color)
            if tile.status_marker else None
        )
        marker_gap = max(2, pad // 2)
        marker_x = right - marker.get_width() if marker is not None else right
        title_max = inner_w
        if marker is not None:
            title_max = max(0, marker_x - marker_gap - left)
        title_text = self._fit_text(
            title_font, tile.compact_title or tile.title, title_max,
        )
        title = title_font.render(title_text, True, tile.title_color)
        specs: list[tuple[str, Any, str, Color, Rect]] = [
            (
                "title", title_font, title_text, tile.title_color,
                Rect(left, top, title.get_width(), title.get_height()),
            ),
        ]
        if marker is not None:
            specs.append(
                (
                    "status", marker_font, tile.status_marker,
                    tile.status_marker_color,
                    Rect(marker_x, top, marker.get_width(), marker.get_height()),
                )
            )

        summary_font = self._font
        summary_gap = max(2, pad // 2)
        summary_top = top + title_font.get_linesize() + summary_gap
        bottom = r.y + r.h - pad
        if tile.bars:
            # Session and weekly are the stable aggregate glance. Scoped and
            # secondary-account bars remain available after tapping the tile.
            summaries = tile.bars[:2]
            for index, bar in enumerate(summaries):
                text = self._ultra_compact_bar_text(bar)
                if summary_font.size(text)[0] > inner_w:
                    # Preserve the percentage itself if the S/W prefix would
                    # make a three-digit value wrap or be truncated.
                    text = bar.percent_text
                text = self._fit_text(summary_font, text, inner_w)
                surface = summary_font.render(text, True, (
                    fmt.mute(bar.color) if bar.muted else bar.color
                ))
                y = summary_top + index * (
                    summary_font.get_linesize() + summary_gap
                )
                if y + surface.get_height() > bottom:
                    break
                specs.append(
                    (
                        f"summary-{index}", summary_font, text,
                        fmt.mute(bar.color) if bar.muted else bar.color,
                        Rect(left, y, surface.get_width(), surface.get_height()),
                    )
                )
        else:
            # Quota-less providers still get a compact status/detail cue rather
            # than a blank tile. It is deliberately one line and has no reset
            # column to collide with it.
            text = self._fit_text(summary_font, tile.detail or "—", inner_w)
            surface = summary_font.render(text, True, tile.detail_color)
            if summary_top + surface.get_height() <= bottom:
                specs.append(
                    (
                        "detail", summary_font, text, tile.detail_color,
                        Rect(
                            left, summary_top,
                            surface.get_width(), surface.get_height(),
                        ),
                    )
                )
        return specs

    def _draw_ultra_compact_tile(self, tile: TileSpec) -> None:
        """Draw a tiny tile without bar/reset rows that can share glyph pixels."""
        for _name, font, text, color, rect in self._ultra_compact_text_specs(tile):
            surface = font.render(text, True, color)
            self._screen.blit(surface, (rect.x, rect.y))

    def _draw_main(self, layout: MainLayout) -> None:
        label_cols = self._label_col_widths(layout.tiles)
        for tile in layout.tiles:
            self._draw_tile(tile, label_cols[tile.compact])
        sr = layout.status_rect
        status_x = sr.x + min(8, max(2, sr.w // 20))
        # Keep the refresh target clear.  At 240px wide the full status string
        # is wider than the remaining footer, so it must be shortened rather
        # than blitted through the button.
        refresh = layout.refresh_rect
        status_right = (
            refresh.x - min(8, max(4, refresh.w // 3))
            if refresh is not None else sr.x + sr.w - status_x
        )
        status = self._font_small.render(
            self._fit_text(self._font_small, layout.status_text,
                           max(0, status_right - status_x)),
            True, fmt.GRAY,
        )
        status_y = sr.y + (sr.h - status.get_height()) // 2
        self._screen.blit(status, (status_x, status_y))
        # Refresh outcome rides alongside the status line for a few seconds, so
        # a tap reports back in words rather than only via the button highlight.
        feedback = self._current_refresh_feedback()
        if feedback:
            colour = fmt.RED if feedback == "refresh failed" else fmt.GRAY
            feedback_x = status_x + status.get_width() + min(8, max(4, sr.w // 40))
            feedback_text = self._fit_text(
                self._font_small, f"· {feedback}",
                max(0, status_right - feedback_x),
            )
            if feedback_text:
                fb = self._font_small.render(feedback_text, True, colour)
                self._screen.blit(fb, (feedback_x, status_y))
        if refresh is not None:
            self._draw_refresh_button(refresh, layout.refresh_pending)

    def _draw_refresh_button(self, rect: Rect, pending: bool) -> None:
        """The status-bar on-demand refresh tap target: a rounded square with a
        circular-arrow icon, highlighted while a refresh POST is in flight so
        the tap reads as acknowledged."""
        br = pygame.Rect(rect.x, rect.y, rect.w, rect.h)
        if pending:
            pygame.draw.rect(self._screen, _REFRESH_PENDING_BG, br, border_radius=8)
            pygame.draw.rect(self._screen, fmt.YELLOW, br, width=2, border_radius=8)
        else:
            pygame.draw.rect(self._screen, _BTN_BG, br, border_radius=8)
            pygame.draw.rect(self._screen, fmt.GRAY, br, width=2, border_radius=8)
        self._draw_refresh_icon(rect, fmt.TEXT)

    def _draw_refresh_icon(self, rect: Rect, color: Color) -> None:
        """Draw the circular-arrow icon with primitives instead of a glyph.

        pygame's bundled freesansbold.ttf has no rotation arrow: U+27F3, U+21BB,
        U+27F2 and U+293A all return .notdef metrics on the unit, so the first
        cut of this button shipped a tofu box to the panel. The rest of the GUI
        sticks to ASCII for exactly this reason (the brightness buttons use
        "-"/"+"), and no Unicode arrow is available to switch to — so draw it.
        """
        cx = rect.x + rect.w / 2
        cy = rect.y + rect.h / 2
        radius = rect.w * 0.28
        thickness = max(2, rect.w // 12)
        box = pygame.Rect(
            round(cx - radius), round(cy - radius), round(radius * 2), round(radius * 2)
        )
        # Leave a gap at the top-right for the arrowhead; pygame measures arc
        # angles counterclockwise from east, so this sweeps from just past the
        # head all the way round.
        pygame.draw.arc(
            self._screen, color, box, math.radians(78), math.radians(358), thickness
        )
        # Arrowhead at the arc's open end. Build it from the tangent/normal at
        # that angle so it points along the stroke (clockwise) and stays
        # correctly oriented at any button size, rather than from hand-tuned
        # pixel offsets.
        head_angle = math.radians(62)
        hx = cx + radius * math.cos(head_angle)
        hy = cy - radius * math.sin(head_angle)
        # Screen y grows downward, so clockwise motion is +(sin, cos).
        tx, ty = math.sin(head_angle), math.cos(head_angle)
        nx, ny = math.cos(head_angle), -math.sin(head_angle)
        size = max(4, rect.w // 6)
        pygame.draw.polygon(
            self._screen,
            color,
            [
                (hx + tx * size * 1.3, hy + ty * size * 1.3),
                (hx - tx * size * 0.4 + nx * size, hy - ty * size * 0.4 + ny * size),
                (hx - tx * size * 0.4 - nx * size, hy - ty * size * 0.4 - ny * size),
            ],
        )

    def _draw_tile(self, tile: TileSpec, label_col_w: int) -> None:
        r = tile.rect
        rect = pygame.Rect(r.x, r.y, r.w, r.h)
        pygame.draw.rect(self._screen, _TILE_BG, rect, border_radius=8)
        pygame.draw.rect(self._screen, tile.accent, rect, width=2, border_radius=8)

        if tile.ultra_compact:
            self._draw_ultra_compact_tile(tile)
            return

        # Consistent padding across all tiles (screen-derived, not per-tile
        # height) so 2-bar and 3-bar tiles have identical spacing.
        pad = self._tile_pad
        inner_left = r.x + pad
        inner_right = r.x + r.w - pad
        header_w = max(0, inner_right - inner_left)
        bar_font = self._bar_font(tile.compact)
        subtitle_w = self._font_small.size(tile.subtitle)[0] if tile.subtitle else 0
        title_w = self._font_title.size(tile.title)[0]
        stack_requested = bool(tile.subtitle) and (
            title_w + subtitle_w + pad > header_w
        )
        # Keep the two header strings on one line when they fit, as on the
        # landscape panel.  On a portrait compact tile the stale/offline suffix
        # is more useful than a clipped title, so move the subtitle below it
        # instead of shortening the provider name to ``ZAI...``.  A very short
        # landscape tile may not have a whole bar row left after that extra
        # line; in that case truncate the subtitle in the title row instead of
        # letting the stacked header collide with bar text.
        stack_subtitle = stack_requested
        if stack_subtitle and tile.bars:
            stacked_top = (
                r.y + pad + self._font_title.get_linesize()
                + self._font_small.get_linesize() + 2 + pad // 2
            )
            content_bottom = r.y + r.h - pad
            stack_subtitle = (
                content_bottom - stacked_top
                >= len(tile.bars) * bar_font.get_linesize()
            )
        if stack_subtitle:
            title_max = header_w
        elif tile.subtitle and not stack_requested:
            title_max = min(title_w, max(0, header_w - subtitle_w - pad))
        else:
            title_max = header_w
        title_text = self._fit_text(self._font_title, tile.title, title_max)
        title_surf = self._font_title.render(title_text, True, tile.title_color)
        self._screen.blit(title_surf, (inner_left, r.y + pad))
        header_extra = 0
        if tile.subtitle:
            if stack_subtitle:
                subtitle_max = header_w
            else:
                subtitle_max = max(0, header_w - title_surf.get_width() - pad)
            subtitle_text = self._fit_text(self._font_small, tile.subtitle, subtitle_max)
            if subtitle_text:
                sub_surf = self._font_small.render(
                    subtitle_text, True, tile.subtitle_color
                )
                if stack_subtitle:
                    subtitle_y = r.y + pad + title_surf.get_height() + 2
                    self._screen.blit(sub_surf, (inner_left, subtitle_y))
                    header_extra = sub_surf.get_height() + 2
                else:
                    self._screen.blit(
                        sub_surf,
                        (
                            inner_right - sub_surf.get_width(),
                            r.y + pad
                            + (title_surf.get_height() - sub_surf.get_height()) // 2,
                        ),
                    )

        # One horizontal row per bar: "Session 49%" | track | "resets 3h 38m".
        content_top = (
            r.y + pad + title_surf.get_height() + header_extra + pad // 2
        )
        bottom = r.y + r.h - pad
        n = max(len(tile.bars), 1)
        row_h = max(1, (bottom - content_top) // n)
        bar_h = max(8, row_h // 3)

        # Fixed columns so bars are uniform across *all* tiles: the label column
        # (*label_col_w*, the widest label fleet-wide) and the reset column (the
        # fixed sentinel width) are both global, so every tile's track starts and
        # ends at the same x. The track ends before the reset column, so a 100%
        # bar lands at that edge and never bleeds into it.
        labels = [
            bar_font.render(
                self._fit_text(
                    bar_font, self._bar_label(bar), label_col_w
                ),
                True,
                fmt.TEXT,
            )
            for bar in tile.bars
        ]
        track_x, track_right = self._bar_track(r, label_col_w, compact=tile.compact)
        # The reset text starts a gap past the bar end, so every reset lines up.
        reset_x = track_right + pad
        track_w = track_right - track_x

        for i, bar in enumerate(tile.bars):
            row_top = content_top + i * row_h
            cy = row_top + row_h // 2  # vertical centre of row

            if track_w < _MIN_BAR_TRACK_W:
                # Last-resort geometry for an unusually narrow/custom window:
                # put the track across the tile and keep the two text columns
                # on the row's upper edge.  The normal 240px fallback avoids
                # this path by stacking paired tiles and using bare resets, but
                # this guard keeps arbitrary small dev windows drawable too.
                text_gap = max(2, pad // 2)
                reset_w = min(
                    self._reset_width(tile.compact), max(0, r.w - 2 * pad)
                )
                label_w = max(0, r.w - 2 * pad - reset_w - text_gap)
                label_text = self._fit_text(
                    bar_font, self._bar_label(bar), label_w
                )
                label = bar_font.render(label_text, True, fmt.TEXT)
                reset_text = self._fit_text(
                    bar_font, self._reset_label(bar, tile.compact), reset_w
                )
                reset = bar_font.render(
                    reset_text,
                    True,
                    fmt.YELLOW if bar.reset_highlight else fmt.GRAY,
                )
                self._screen.blit(label, (r.x + pad, row_top))
                self._screen.blit(
                    reset, (r.x + r.w - pad - reset.get_width(), row_top)
                )
                row_bottom = min(bottom, row_top + row_h)
                text_bottom = row_top + max(label.get_height(), reset.get_height())
                fallback_track_y = text_bottom + text_gap
                fallback_track_h = min(
                    bar_h, max(0, row_bottom - fallback_track_y)
                )
                if fallback_track_h <= 0:
                    # There is no room for a track below the two text columns.
                    # Omitting it is preferable to painting a bar through the
                    # next row's text on an arbitrarily tiny custom window.
                    continue
                fallback_track_w = max(1, r.w - 2 * pad)
                pygame.draw.rect(
                    self._screen,
                    fmt.BAR_BG,
                    pygame.Rect(
                        r.x + pad, fallback_track_y,
                        fallback_track_w, fallback_track_h,
                    ),
                    border_radius=4,
                )
                fill_w = max(0, int(fallback_track_w * bar.fraction))
                if fill_w > 0:
                    fill_color = fmt.mute(bar.color) if bar.muted else bar.color
                    pygame.draw.rect(
                        self._screen,
                        fill_color,
                        pygame.Rect(
                            r.x + pad, fallback_track_y, fill_w, fallback_track_h
                        ),
                        border_radius=4,
                    )
                continue

            self._screen.blit(labels[i], (r.x + pad, cy - labels[i].get_height() // 2))

            track_y = cy - bar_h // 2
            pygame.draw.rect(
                self._screen,
                fmt.BAR_BG,
                pygame.Rect(track_x, track_y, track_w, bar_h),
                border_radius=4,
            )
            fill_w = max(0, int(track_w * bar.fraction))
            if fill_w > 0:
                fill_color = fmt.mute(bar.color) if bar.muted else bar.color
                pygame.draw.rect(
                    self._screen,
                    fill_color,
                    pygame.Rect(track_x, track_y, fill_w, bar_h),
                    border_radius=4,
                )

            if bar.reset_text:
                rc = fmt.YELLOW if bar.reset_highlight else fmt.GRAY
                reset_text = self._fit_text(
                    bar_font,
                    self._reset_label(bar, tile.compact),
                    self._reset_width(tile.compact),
                )
                reset = bar_font.render(reset_text, True, rc)
                # Left-aligned at a fixed x just past the bar, so resets line up.
                self._screen.blit(reset, (reset_x, cy - reset.get_height() // 2))

        # Quota-less providers carry their signal in ``detail`` instead of bars;
        # without this the tile would render as a bare title. Vertically centred
        # in the content area, coloured by the volume alert (see layout).
        if tile.detail and not tile.bars:
            detail_text = self._fit_text(
                self._font, tile.detail, max(0, inner_right - inner_left)
            )
            detail_surf = self._font.render(detail_text, True, tile.detail_color)
            self._screen.blit(
                detail_surf,
                (
                    inner_left,
                    content_top + (bottom - content_top - detail_surf.get_height()) // 2,
                ),
            )

    def _draw_detail(self, readings: list[Reading]) -> None:
        by_provider = {r.provider: r for r in readings}
        reading = by_provider.get(self._state.detail_provider)  # type: ignore[arg-type]
        if reading is None:
            self._state = ViewState()
            return
        # Fold the work Claude account into the Claude detail view, if present.
        secondary = None
        if reading.provider is Provider.CLAUDE:
            work = by_provider.get(Provider.CLAUDE_WORK)
            if work is not None:
                secondary = ("work", work)
        detail: DetailLayout = build_detail_layout(reading, secondary=secondary)
        pad = max(10, self._width // 30)
        content_w = max(0, self._width - 2 * pad)
        title_text = self._fit_text(self._font_title, detail.title, content_w)
        title = self._font_title.render(title_text, True, fmt.TEXT)
        self._screen.blit(title, (pad, pad))
        y = pad + title.get_height() + pad
        hint_text = self._fit_text(
            self._font_small, "tap anywhere to go back", content_w
        )
        hint = self._font_small.render(hint_text, True, fmt.GRAY)
        bottom_limit = self._height - hint.get_height() - pad * 2
        line_gap = max(3, min(6, self._font.get_linesize() // 5))
        font_h = self._font.get_linesize()
        for line in detail.lines:
            raw_label = f"{line.label}:" if line.label else ""
            label_text = self._fit_text(self._font, raw_label, content_w)
            label_surf = self._font.render(label_text, True, line.color)
            value_text = self._fit_text(self._font, line.value, content_w)
            value_surf = self._font.render(value_text, True, line.color)
            inline = bool(line.value) and (
                not line.label
                or label_surf.get_width() + line_gap + value_surf.get_width()
                <= content_w
            )
            needed = font_h if inline or not line.value else 2 * font_h + line_gap
            if y + needed > bottom_limit:
                ellipsis = self._font.render(
                    self._fit_text(self._font, "...", content_w), True, fmt.GRAY
                )
                if y + ellipsis.get_height() <= bottom_limit:
                    self._screen.blit(ellipsis, (pad, y))
                break
            self._screen.blit(label_surf, (pad, y))
            if line.value:
                if inline:
                    self._screen.blit(
                        value_surf,
                        (self._width - pad - value_surf.get_width(), y),
                    )
                else:
                    # A long value (model names and timestamps are common
                    # examples) gets its own line instead of being painted over
                    # the label or clipped past the right edge.
                    self._screen.blit(value_surf, (pad, y + font_h + line_gap))
            y += needed + line_gap
        self._screen.blit(hint, (pad, self._height - hint.get_height() - pad))

    def _draw_status_overlay(self) -> None:
        overlay: StatusOverlay = build_status_overlay((self._width, self._height))
        p = overlay.panel
        panel_rect = pygame.Rect(p.x, p.y, p.w, p.h)
        pygame.draw.rect(self._screen, _OVERLAY_BG, panel_rect, border_radius=12)
        pygame.draw.rect(self._screen, fmt.TEXT, panel_rect, width=2, border_radius=12)
        pad = status_overlay_padding((self._width, self._height))
        # A hairline divider between the diagnostics and brightness columns.
        div_x = overlay.brightness.region.x - pad
        pygame.draw.line(
            self._screen,
            _BTN_BG,
            (div_x, overlay.diag_rect.y),
            (div_x, overlay.diag_rect.y + overlay.diag_rect.h),
            1,
        )
        self._draw_diagnostics(overlay.diag_rect)
        self._draw_brightness_controls(overlay.brightness)

        footer_h = max(
            status_overlay_footer_height((self._width, self._height)),
            self._font_small.get_height() + 4,
        )
        hint = self._font_small.render(
            self._fit_text(self._font_small, "tap outside to close", max(0, p.w - 2 * pad)),
            True,
            fmt.GRAY,
        )
        hint_y = p.y + p.h - pad - footer_h + max(0, (footer_h - hint.get_height()) // 2)
        self._screen.blit(
            hint,
            (p.x + (p.w - hint.get_width()) // 2, hint_y),
        )

    def _draw_diagnostics(self, rect: Rect) -> None:
        """Left column: how to reach this unit and whether the updater is happy."""
        title = self._font_title.render(
            self._fit_text(self._font_title, "This unit", rect.w), True, fmt.TEXT
        )
        self._screen.blit(title, (rect.x, rect.y))
        y = rect.y + title.get_height() + max(3, min(6, self._font.get_linesize() // 5))
        lines = (
            diag.diagnostic_lines(self._diag, datetime.now(timezone.utc))
            if self._diag is not None else []
        )
        line_gap = max(3, min(6, self._font.get_linesize() // 5))
        font_h = self._font.get_linesize()
        for line in lines:
            value_color = fmt.RED if line.warn else fmt.TEXT
            label = self._font.render(
                self._fit_text(self._font, line.label, rect.w), True, fmt.GRAY
            )
            value = self._font.render(
                self._fit_text(self._font, line.value, rect.w), True, value_color
            )
            inline = bool(line.value) and (
                not line.label
                or label.get_width() + line_gap + value.get_width() <= rect.w
            )
            needed = font_h if inline or not line.value else 2 * font_h + line_gap
            if y + needed > rect.y + rect.h:
                ellipsis = self._font.render(
                    self._fit_text(self._font, "...", rect.w), True, fmt.GRAY
                )
                if y + ellipsis.get_height() <= rect.y + rect.h:
                    self._screen.blit(ellipsis, (rect.x, y))
                break
            if line.label:
                self._screen.blit(label, (rect.x, y))
            if line.value:
                if inline:
                    self._screen.blit(
                        value, (rect.x + rect.w - value.get_width(), y)
                    )
                else:
                    self._screen.blit(value, (rect.x, y + font_h + line_gap))
            y += needed + line_gap

    def _draw_brightness_controls(self, controls: BrightnessOverlay) -> None:
        """Right column: ``−`` | step readout + gauge | ``+``."""
        region = controls.region
        title = self._font_title.render(
            self._fit_text(self._font_title, "Brightness", region.w), True, fmt.TEXT
        )
        self._screen.blit(
            title, (region.x + (region.w - title.get_width()) // 2, region.y)
        )
        # Big finger targets. ASCII glyphs so the default font always has them.
        for rect, glyph in ((controls.minus, "-"), (controls.plus, "+")):
            br = pygame.Rect(rect.x, rect.y, rect.w, rect.h)
            pygame.draw.rect(self._screen, _BTN_BG, br, border_radius=10)
            pygame.draw.rect(self._screen, fmt.GRAY, br, width=2, border_radius=10)
            glyph_text = self._fit_text(self._font_big, glyph, rect.w)
            g = self._font_big.render(glyph_text, True, fmt.TEXT)
            self._screen.blit(g, (rect.x + (rect.w - g.get_width()) // 2,
                                  rect.y + (rect.h - g.get_height()) // 2))

        # Centre readout: current step over a filled-segment gauge. "—" when
        # there's no controllable backlight, so the inert +/- read as inert.
        lr = controls.level_rect
        cx = lr.x + lr.w // 2
        available = self._backlight.available
        steps = self._brightness_steps
        gap = 3
        # Four pixels is enough for the gauge on the very short 320x240 card;
        # preserving that space lets the actual step glyph remain visible.
        seg_h = min(max(4, lr.h // 8), max(1, lr.h))
        seg_y = lr.y + lr.h - seg_h

        # Reserve the gauge before placing the readout.  On a short landscape
        # card the denominator is deliberately dropped when it cannot fit; a
        # clipped ``of 10`` is less readable than the step number and would
        # overlap the gauge.
        num_text = str(self._brightness_step) if available else "—"
        readout_font = (
            self._font
            if lr.h
            < self._font_big.get_linesize() + self._font_small.get_linesize() + 12
            else self._font_big
        )
        num = readout_font.render(
            self._fit_text(readout_font, num_text, max(0, lr.w)), True, fmt.TEXT
        )
        denominator = f"of {self._brightness_steps}" if available else ""
        if denominator and self._font_small.size(denominator)[0] > lr.w:
            # The centre column can be only a few pixels wide on a 240px
            # display.  The step count alone still explains the gauge without
            # spilling into the +/- targets.
            denominator = str(self._brightness_steps)
        den = self._font_small.render(
            self._fit_text(self._font_small, denominator, max(0, lr.w)),
            True,
            fmt.GRAY,
        )
        text_bottom = seg_y - 2
        num_y = lr.y + max(0, (text_bottom - lr.y - num.get_height()) // 2)
        num_fits = num_y + num.get_height() <= text_bottom
        if num_fits:
            self._screen.blit(num, (cx - num.get_width() // 2, num_y))
        if available and num_fits:
            den_y = num_y + num.get_height() + 2
            if den_y + den.get_height() <= text_bottom:
                self._screen.blit(den, (cx - den.get_width() // 2, den_y))

        required = steps * 2 + (steps - 1) * gap
        if lr.w < required:
            # A segmented gauge cannot fit in the narrow centre column.  Use a
            # single filled track instead of letting the segment math overlap
            # the +/- controls.
            gauge = pygame.Rect(lr.x, seg_y, max(1, lr.w), seg_h)
            pygame.draw.rect(
                self._screen, fmt.BAR_BG, gauge, border_radius=2
            )
            if available:
                fill_w = max(0, int(gauge.w * self._brightness_step / steps))
                if fill_w:
                    pygame.draw.rect(
                        self._screen, fmt.GREEN,
                        pygame.Rect(gauge.x, gauge.y, fill_w, gauge.h),
                        border_radius=2,
                    )
            return

        seg_w = max(2, (lr.w - (steps - 1) * gap) // steps)
        total_w = seg_w * steps + gap * (steps - 1)
        seg_x = lr.x + (lr.w - total_w) // 2
        for i in range(steps):
            lit = available and i < self._brightness_step
            color = fmt.GREEN if lit else fmt.BAR_BG
            pygame.draw.rect(
                self._screen, color,
                pygame.Rect(seg_x + i * (seg_w + gap), seg_y, seg_w, seg_h),
                border_radius=2,
            )


def _default_brightness_state_file() -> Path | None:
    """Where a manually-chosen brightness is remembered across reboots: the
    ``brightness`` file in the shared per-unit state dir (see
    :func:`diagnostics.default_state_dir`). None if the dir can't be resolved
    (persistence then degrades to off, harmlessly)."""
    state_dir = diag.default_state_dir()
    return state_dir / "brightness" if state_dir is not None else None


def _init_display() -> tuple[int, int]:
    fullscreen = os.environ.get("GUI_FULLSCREEN", "1") != "0"
    # A finger tap otherwise fires BOTH a FINGERDOWN and a synthesized
    # MOUSEBUTTONDOWN; handling both toggles the view twice and a tap looks
    # like a no-op. Keep touch and mouse as distinct event sources.
    os.environ.setdefault("SDL_TOUCH_MOUSE_EVENTS", "0")
    pygame.init()
    pygame.font.init()
    if fullscreen:
        info = pygame.display.Info()
        size = (info.current_w, info.current_h)
        pygame.display.set_mode(size, pygame.FULLSCREEN)
        pygame.mouse.set_visible(False)
    else:
        size = (_env_int("GUI_WIDTH", 800), _env_int("GUI_HEIGHT", 480))
        pygame.display.set_mode(size)
    pygame.display.set_caption("AI Usage")
    return size


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server_url = os.environ.get("SERVER_URL", "")
    api_key = os.environ.get("API_KEY", "")
    if not server_url or not api_key:
        logger.error("SERVER_URL and API_KEY environment variables are required")
        sys.exit(1)

    size = _init_display()
    # Backlight sleep is opt-in (BACKLIGHT_SLEEP=1) so an auto-update rollout
    # doesn't start blanking panels until each unit is configured and the
    # backlight node is confirmed writable. When enabled, the active schedule is
    # resolved each frame from the server (per UNIT_ID) > BACKLIGHT_SCHEDULE env
    # > built-in default, so a remote ConfigMap edit applies without a restart.
    sleep_enabled = _env_bool("BACKLIGHT_SLEEP")
    unit_id = os.environ.get("UNIT_ID") or None
    fetcher = ClientFetcher(
        server_url=server_url, api_key=api_key,
        unit_id=unit_id, fetch_schedule=sleep_enabled,
    )
    resolver = (
        ScheduleResolver(env_spec=os.environ.get("BACKLIGHT_SCHEDULE") or None)
        if sleep_enabled else None
    )
    # Brightness control (tap the status line): BRIGHTNESS_STEPS tunes how many
    # +/- notches span dim→full; BRIGHTNESS_STATE_FILE overrides where the chosen
    # level is remembered across reboots (empty string disables persistence).
    state_env = os.environ.get("BRIGHTNESS_STATE_FILE")
    if state_env is None:
        brightness_state_file: Path | None = _default_brightness_state_file()
    else:
        brightness_state_file = Path(state_env) if state_env.strip() else None
    gui = DashboardGui(
        fetcher,
        size,
        fps=_env_int("GUI_FPS", 10),
        touch_rotate=_env_int("GUI_TOUCH_ROTATE", 0),
        schedule_resolver=resolver,
        brightness_steps=_env_int("BRIGHTNESS_STEPS", 10),
        brightness_state_file=brightness_state_file,
        server_url=server_url,
        status_dir=diag.default_state_dir(),
    )

    def _handle_sigterm(signum: int, frame: Any) -> None:
        logger.info("Received SIGTERM, shutting down")
        gui.stop()

    signal.signal(signal.SIGTERM, _handle_sigterm)

    fetcher.start()
    logger.info("GUI started, polling %s", server_url)
    try:
        gui.run()
    except KeyboardInterrupt:
        pass
    finally:
        fetcher.stop()
        pygame.quit()
        logger.info("GUI stopped")


if __name__ == "__main__":
    main()
