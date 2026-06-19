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
        unit = max(12, self._height // 24)
        self._font = pygame.font.Font(None, unit)
        self._font_small = pygame.font.Font(None, max(10, unit * 3 // 4))
        self._font_title = pygame.font.Font(None, unit * 3 // 2)

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
            layout = build_main_layout(readings, (self._width, self._height))
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
        status = self._font_small.render(layout.status_text, True, fmt.GRAY)
        sr = layout.status_rect
        self._screen.blit(status, (sr.x + 8, sr.y + (sr.h - status.get_height()) // 2))

    def _draw_tile(self, tile: TileSpec) -> None:
        r = tile.rect
        rect = pygame.Rect(r.x, r.y, r.w, r.h)
        pygame.draw.rect(self._screen, _TILE_BG, rect, border_radius=8)
        pygame.draw.rect(self._screen, tile.accent, rect, width=2, border_radius=8)

        pad = max(6, r.w // 20)
        self._screen.blit(
            self._font.render(tile.title, True, fmt.TEXT), (r.x + pad, r.y + pad)
        )

        if tile.detail is not None:
            det = self._font_small.render(tile.detail, True, fmt.GRAY)
            self._screen.blit(det, (r.x + pad, r.y + r.h // 2))
            return

        # Two stacked bars in the lower portion of the tile.
        bar_top = r.y + pad + self._font.get_height() + pad
        row_h = (r.y + r.h - pad - bar_top) // max(len(tile.bars), 1)
        track_x = r.x + pad
        track_w = r.w - pad * 2
        bar_h = max(6, row_h // 4)
        for i, bar in enumerate(tile.bars):
            row_y = bar_top + i * row_h
            # Tag a second account's bars (e.g. "work · Session"); single
            # account leaves account empty, so the label is unchanged.
            label_text = f"{bar.account} · {bar.label}" if bar.account else bar.label
            label = self._font_small.render(
                f"{label_text} {bar.percent_text}", True, fmt.TEXT
            )
            self._screen.blit(label, (track_x, row_y))
            track_y = row_y + label.get_height() + 2
            pygame.draw.rect(
                self._screen, fmt.BAR_BG,
                pygame.Rect(track_x, track_y, track_w, bar_h), border_radius=3,
            )
            fill_w = max(0, int(track_w * bar.fraction))
            if fill_w > 0:
                fill_color = fmt.mute(bar.color) if bar.muted else bar.color
                pygame.draw.rect(
                    self._screen, fill_color,
                    pygame.Rect(track_x, track_y, fill_w, bar_h), border_radius=3,
                )
            if bar.reset_text:
                color = fmt.YELLOW if bar.reset_highlight else fmt.GRAY
                reset = self._font_small.render(f"resets {bar.reset_text}", True, color)
                self._screen.blit(reset, (track_x, track_y + bar_h + 2))

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
            text = self._font.render(f"{line.label}:  {line.value}", True, line.color)
            self._screen.blit(text, (pad, y))
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
