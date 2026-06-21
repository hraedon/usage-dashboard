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
from typing import Any

import pygame

from usage_dashboard.client import format as fmt
from usage_dashboard.client.fetcher import ClientFetcher
from usage_dashboard.client.layout import (
    DetailLayout,
    MainLayout,
    TileSpec,
    ViewState,
    build_detail_layout,
    build_main_layout,
    rotate_touch_norm,
    tap_transition,
)
from usage_dashboard.shared.models import Provider, Reading

logger = logging.getLogger(__name__)

_TILE_BG = (17, 17, 17)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


class DashboardGui:
    """Owns the pygame window, fonts, view state, and render loop."""

    def __init__(
        self,
        fetcher: ClientFetcher,
        size: tuple[int, int],
        fps: int = 10,
        touch_rotate: int = 0,
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
        # Fonts scaled to the panel so the same code reads on any resolution.
        # Sized for a 5" panel read at arm's length — bigger than a desktop UI.
        unit = max(18, self._height // 15)
        self._font = pygame.font.Font(None, unit)
        self._font_small = pygame.font.Font(None, max(14, unit * 4 // 5))
        self._font_title = pygame.font.Font(None, unit * 5 // 4)
        # Fixed width reserved on the right of every bar row for the reset text,
        # sized to a worst-case countdown so the bar track always ends at the
        # same x and never bleeds into "resets …".
        self._reset_col_w = self._font.size("resets 23d 23h")[0]

    # -- event loop ---------------------------------------------------------

    def stop(self) -> None:
        self._running = False

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

    def _handle_events(self, layout: MainLayout) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self._running = False
            elif event.type == pygame.KEYDOWN and event.key in (
                pygame.K_ESCAPE, pygame.K_q
            ):
                self._running = False
            else:
                pos = self._tap_position(event)
                if pos is not None:
                    self._state = tap_transition(self._state, layout, pos)

    def run(self) -> None:
        while self._running:
            readings = self._fetcher.get_latest_readings()
            layout = build_main_layout(
                readings, (self._width, self._height),
                refresh_interval=self._fetcher.current_interval,
            )
            self._handle_events(layout)
            self._screen.fill(fmt.BG)
            if self._state.detail_provider is None:
                self._draw_main(layout)
            else:
                self._draw_detail(readings)
            pygame.display.flip()
            self._clock.tick(self._fps)

    # -- rendering ----------------------------------------------------------

    def _draw_main(self, layout: MainLayout) -> None:
        for tile in layout.tiles:
            self._draw_tile(tile)
        sr = layout.status_rect
        status = self._font_small.render(layout.status_text, True, fmt.GRAY)
        self._screen.blit(status, (sr.x + 8, sr.y + (sr.h - status.get_height()) // 2))
        # Quota-less umans summary sits in the status bar, next to the timer.
        if layout.footer_note:
            note = self._font_title.render(layout.footer_note, True, fmt.TEXT)
            self._screen.blit(
                note,
                (sr.x + sr.w - note.get_width() - 12,
                 sr.y + (sr.h - note.get_height()) // 2),
            )

    def _draw_tile(self, tile: TileSpec) -> None:
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

        # Fixed columns so every bar in the tile is the same length and the
        # resets right-align: label column = widest label in this tile, reset
        # column = the fixed sentinel width. The track ends before the reset
        # column, so a 100% bar lands at that edge and never bleeds into it.
        labels = [
            self._font.render(
                f"{(bar.account + ' · ') if bar.account else ''}"
                f"{bar.label} {bar.percent_text}",
                True, fmt.TEXT,
            )
            for bar in tile.bars
        ]
        label_col_w = max((s.get_width() for s in labels), default=0)
        track_x = r.x + pad + label_col_w + pad
        # Reserve the reset column (sentinel width) with a gap each side, so the
        # bar ends before it and every reset starts at the same x, abutting the bar.
        track_right = r.x + r.w - pad - self._reset_col_w - pad
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
                reset = self._font.render(f"resets {bar.reset_text}", True, rc)
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
        for line in detail.lines:
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
    fetcher = ClientFetcher(server_url=server_url, api_key=api_key)
    gui = DashboardGui(
        fetcher,
        size,
        fps=_env_int("GUI_FPS", 10),
        touch_rotate=_env_int("GUI_TOUCH_ROTATE", 0),
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
