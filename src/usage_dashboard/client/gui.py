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
import os
import signal
import sys
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
    DetailLayout,
    MainLayout,
    Rect,
    StatusOverlay,
    TileSpec,
    ViewState,
    build_detail_layout,
    build_main_layout,
    build_status_overlay,
    rotate_touch_norm,
    tap_transition,
)
from usage_dashboard.client.schedule import ScheduleResolver, SleepSchedule
from usage_dashboard.shared.models import Provider, Reading

logger = logging.getLogger(__name__)

_TILE_BG = (17, 17, 17)
_OVERLAY_BG = (28, 28, 28)
_BTN_BG = (45, 45, 45)


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
        # While dark we tick slowly to save CPU but still pump touch events.
        self._sleep_fps = 4
        # Fonts scaled to the panel so the same code reads on any resolution.
        # Sized for a 5" panel read at arm's length — bigger than a desktop UI.
        unit = max(18, self._height // 15)
        self._font = pygame.font.Font(None, unit)
        self._font_small = pygame.font.Font(None, max(14, unit * 4 // 5))
        self._font_title = pygame.font.Font(None, unit * 5 // 4)
        # Oversized glyphs for the brightness overlay's +/- buttons and readout.
        self._font_big = pygame.font.Font(None, unit * 2)
        # Fixed width reserved on the right of every bar row for the reset text,
        # sized to a worst-case countdown so the bar track always ends at the
        # same x and never bleeds into the countdown. Full-width tiles show
        # "resets 23d 23h"; compact (paired) tiles show a bare "23d 23h", so
        # their column is narrower and the bar track extends further.
        self._reset_col_w = self._font.size("resets 23d 23h")[0]
        self._compact_reset_col_w = self._font.size("23d 23h")[0]

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

    def _bar_label(self, bar: BarSpec) -> str:
        """The left-column text for a bar: ``[account · ]Label NN%``."""
        return (
            f"{(bar.account + ' · ') if bar.account else ''}"
            f"{bar.label} {bar.percent_text}"
        )

    def _label_col_widths(self, tiles: list[TileSpec]) -> dict[bool, int]:
        """Widest bar label per tile group (compact vs full-width).

        Full-width tiles (Session/Weekly) and compact tiles (S/W) are sized
        independently so each group's bars start at the same x without the
        wide labels stealing space from the narrow paired tiles."""
        widths: dict[bool, int] = {False: 0, True: 0}
        for tile in tiles:
            for bar in tile.bars:
                w = self._font.size(self._bar_label(bar))[0]
                if w > widths[tile.compact]:
                    widths[tile.compact] = w
        return widths

    def _bar_track(self, rect: Rect, label_col_w: int, compact: bool = False) -> tuple[int, int]:
        """``(track_x, track_right)`` for a tile's bars given the label column
        width. Compact tiles use a narrower reset column (bare countdown, no
        "resets" prefix) so the bar track extends further."""
        pad = max(8, min(rect.w, rect.h) // 12)
        reset_w = self._compact_reset_col_w if compact else self._reset_col_w
        track_x = rect.x + pad + label_col_w + pad
        track_right = rect.x + rect.w - pad - reset_w - pad
        return track_x, track_right

    def _draw_main(self, layout: MainLayout) -> None:
        label_cols = self._label_col_widths(layout.tiles)
        for tile in layout.tiles:
            self._draw_tile(tile, label_cols[tile.compact])
        sr = layout.status_rect
        status = self._font_small.render(layout.status_text, True, fmt.GRAY)
        self._screen.blit(status, (sr.x + 8, sr.y + (sr.h - status.get_height()) // 2))
        # Quota-less umans summary sits in the status bar, next to the timer.
        if layout.footer_note:
            note = self._font_title.render(layout.footer_note, True, layout.footer_color)
            self._screen.blit(
                note,
                (sr.x + sr.w - note.get_width() - 12,
                 sr.y + (sr.h - note.get_height()) // 2),
            )

    def _draw_tile(self, tile: TileSpec, label_col_w: int) -> None:
        r = tile.rect
        rect = pygame.Rect(r.x, r.y, r.w, r.h)
        pygame.draw.rect(self._screen, _TILE_BG, rect, border_radius=8)
        pygame.draw.rect(self._screen, tile.accent, rect, width=2, border_radius=8)

        # Pad off the smaller dimension so wide, short stacked tiles aren't
        # over-padded (r.w//20 was huge once tiles span the full width).
        pad = max(8, min(r.w, r.h) // 12)
        title_surf = self._font_title.render(tile.title, True, fmt.TEXT)
        self._screen.blit(title_surf, (r.x + pad, r.y + pad))
        if tile.subtitle:
            sub_surf = self._font_small.render(tile.subtitle, True, fmt.GRAY)
            self._screen.blit(
                sub_surf,
                (r.x + r.w - pad - sub_surf.get_width(),
                 r.y + pad + (title_surf.get_height() - sub_surf.get_height()) // 2),
            )

        # One horizontal row per bar: "Session 49%" | track | "resets 3h 38m".
        content_top = r.y + pad + title_surf.get_height() + pad // 2
        bottom = r.y + r.h - pad
        n = max(len(tile.bars), 1)
        row_h = (bottom - content_top) // n
        bar_h = max(8, row_h // 3)

        # Fixed columns so bars are uniform across *all* tiles: the label column
        # (*label_col_w*, the widest label fleet-wide) and the reset column (the
        # fixed sentinel width) are both global, so every tile's track starts and
        # ends at the same x. The track ends before the reset column, so a 100%
        # bar lands at that edge and never bleeds into it.
        labels = [
            self._font.render(self._bar_label(bar), True, fmt.TEXT)
            for bar in tile.bars
        ]
        track_x, track_right = self._bar_track(r, label_col_w, compact=tile.compact)
        # The reset text starts a gap past the bar end, so every reset lines up.
        reset_x = track_right + pad
        track_w = track_right - track_x

        for i, bar in enumerate(tile.bars):
            cy = content_top + i * row_h + row_h // 2  # vertical centre of row
            self._screen.blit(labels[i], (r.x + pad, cy - labels[i].get_height() // 2))

            if track_w > 20:
                track_y = cy - bar_h // 2
                pygame.draw.rect(
                    self._screen, fmt.BAR_BG,
                    pygame.Rect(track_x, track_y, track_w, bar_h), border_radius=4,
                )
                fill_w = max(0, int(track_w * bar.fraction))
                if fill_w > 0:
                    fill_color = fmt.mute(bar.color) if bar.muted else bar.color
                    pygame.draw.rect(
                        self._screen, fill_color,
                        pygame.Rect(track_x, track_y, fill_w, bar_h), border_radius=4,
                    )

            if bar.reset_text:
                rc = fmt.YELLOW if bar.reset_highlight else fmt.GRAY
                text = bar.reset_text if tile.compact else f"resets {bar.reset_text}"
                reset = self._font.render(text, True, rc)
                # Left-aligned at a fixed x just past the bar, so resets line up.
                self._screen.blit(reset, (reset_x, cy - reset.get_height() // 2))

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
        self._screen.blit(
            self._font_title.render(detail.title, True, fmt.TEXT), (pad, pad)
        )
        y = pad + self._font_title.get_height() + pad
        bottom_limit = self._height - self._font_small.get_height() - pad * 2
        for line in detail.lines:
            if y + self._font.get_height() > bottom_limit:
                self._screen.blit(
                    self._font.render("…", True, fmt.GRAY), (pad, y)
                )
                break
            label_surf = self._font.render(f"{line.label}:", True, line.color)
            self._screen.blit(label_surf, (pad, y))
            if line.value:
                val_surf = self._font.render(line.value, True, line.color)
                self._screen.blit(
                    val_surf, (self._width - pad - val_surf.get_width(), y)
                )
            y += self._font.get_height() + 6
        hint = self._font_small.render("tap anywhere to go back", True, fmt.GRAY)
        self._screen.blit(hint, (pad, self._height - hint.get_height() - pad))

    def _draw_status_overlay(self) -> None:
        overlay: StatusOverlay = build_status_overlay((self._width, self._height))
        p = overlay.panel
        panel_rect = pygame.Rect(p.x, p.y, p.w, p.h)
        pygame.draw.rect(self._screen, _OVERLAY_BG, panel_rect, border_radius=12)
        pygame.draw.rect(self._screen, fmt.TEXT, panel_rect, width=2, border_radius=12)
        pad = max(8, min(p.w, p.h) // 14)
        # A hairline divider between the diagnostics and brightness columns.
        div_x = overlay.brightness.region.x - pad
        pygame.draw.line(
            self._screen, _BTN_BG, (div_x, p.y + pad), (div_x, p.y + p.h - pad), 1
        )
        self._draw_diagnostics(overlay.diag_rect)
        self._draw_brightness_controls(overlay.brightness)

        hint = self._font_small.render("tap outside to close", True, fmt.GRAY)
        self._screen.blit(
            hint,
            (p.x + (p.w - hint.get_width()) // 2, p.y + p.h - hint.get_height() - 4),
        )

    def _draw_diagnostics(self, rect: Rect) -> None:
        """Left column: how to reach this unit and whether the updater is happy."""
        title = self._font_title.render("This unit", True, fmt.TEXT)
        self._screen.blit(title, (rect.x, rect.y))
        y = rect.y + title.get_height() + 6
        lines = (
            diag.diagnostic_lines(self._diag, datetime.now(timezone.utc))
            if self._diag is not None else []
        )
        for line in lines:
            if y + self._font.get_height() > rect.y + rect.h:
                break
            value_color = fmt.RED if line.warn else fmt.TEXT
            if line.label:
                self._screen.blit(
                    self._font.render(f"{line.label}", True, fmt.GRAY), (rect.x, y)
                )
            if line.value:
                val = self._font.render(line.value, True, value_color)
                self._screen.blit(
                    val, (rect.x + rect.w - val.get_width(), y)
                )
            y += self._font.get_height() + 6

    def _draw_brightness_controls(self, controls: BrightnessOverlay) -> None:
        """Right column: ``−`` | step readout + gauge | ``+``."""
        region = controls.region
        title = self._font_title.render("Brightness", True, fmt.TEXT)
        self._screen.blit(
            title, (region.x + (region.w - title.get_width()) // 2, region.y)
        )
        # Big finger targets. ASCII glyphs so the default font always has them.
        for rect, glyph in ((controls.minus, "-"), (controls.plus, "+")):
            br = pygame.Rect(rect.x, rect.y, rect.w, rect.h)
            pygame.draw.rect(self._screen, _BTN_BG, br, border_radius=10)
            pygame.draw.rect(self._screen, fmt.GRAY, br, width=2, border_radius=10)
            g = self._font_big.render(glyph, True, fmt.TEXT)
            self._screen.blit(
                g,
                (rect.x + (rect.w - g.get_width()) // 2,
                 rect.y + (rect.h - g.get_height()) // 2),
            )

        # Centre readout: current step over a filled-segment gauge. "—" when
        # there's no controllable backlight, so the inert +/- read as inert.
        lr = controls.level_rect
        cx = lr.x + lr.w // 2
        available = self._backlight.available
        num_text = str(self._brightness_step) if available else "—"
        num = self._font_big.render(num_text, True, fmt.TEXT)
        den = self._font_small.render(f"of {self._brightness_steps}", True, fmt.GRAY)
        self._screen.blit(num, (cx - num.get_width() // 2, lr.y + 4))
        if available:
            self._screen.blit(
                den, (cx - den.get_width() // 2, lr.y + 4 + num.get_height())
            )
        steps = self._brightness_steps
        gap = 3
        seg_w = max(2, (lr.w - (steps - 1) * gap) // steps)
        seg_h = max(6, lr.h // 8)
        total_w = seg_w * steps + gap * (steps - 1)
        seg_x = lr.x + (lr.w - total_w) // 2
        seg_y = lr.y + lr.h - seg_h
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
