"""Headless smoke tests for the pygame GUI.

These run the real draw path under SDL's dummy video driver (no display), so a
broken blit/geometry call is caught in CI without hardware. Skipped entirely
when the optional ``gui`` extra (pygame) isn't installed.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

pygame = pytest.importorskip("pygame")

from usage_dashboard.client import diagnostics  # noqa: E402
from usage_dashboard.client.brightness import step_for_level  # noqa: E402
from usage_dashboard.client.gui import (  # noqa: E402
    DashboardGui,
    DoubleTapDetector,
)
from usage_dashboard.client.layout import (  # noqa: E402
    MIN_TOUCH_TARGET,
    ViewState,
    build_main_layout,
    build_status_overlay,
    hit_test,
)
from usage_dashboard.shared.models import (  # noqa: E402
    Provider,
    Reading,
    ReadingStatus,
    ScopedLimit,
)

_NOW = datetime(2026, 1, 10, 12, 0, 0)
# Include both orientations of the two phone-sized fallbacks and the real
# 720x1280 panel.  In particular, 390x844 is a real render regression rather
# than a layout-only smoke check: its stacked tile headers used to overlap bars.
_AUDIT_SIZES = (
    (240, 320), (390, 844), (720, 1280),
    (320, 240), (844, 390), (1280, 720),
)


def _readings() -> list[Reading]:
    def r(provider: Provider, **over: object) -> Reading:
        base = {
            "provider": provider,
            "status": ReadingStatus.CURRENT,
            "session_percent": 50.0,
            "session_resets_at": _NOW.replace(tzinfo=timezone.utc),
            "weekly_percent": 90.0,
            "weekly_resets_at": _NOW.replace(tzinfo=timezone.utc),
            "fetched_at": _NOW,
            "stale": False,
        }
        base.update(over)
        return Reading(**base)  # type: ignore[arg-type]

    return [
        r(Provider.CLAUDE),
        r(Provider.ZAI, status=ReadingStatus.STALE, stale=True),
        r(Provider.OLLAMA, session_percent=None, weekly_percent=None),
    ]


def _four_provider_readings() -> list[Reading]:
    """A real four-tile fleet, including one quota-less provider."""
    codex = Reading(
        provider=Provider.CODEX,
        status=ReadingStatus.CURRENT,
        session_percent=100.0,
        session_resets_at=_NOW.replace(tzinfo=timezone.utc),
        weekly_percent=100.0,
        weekly_resets_at=_NOW.replace(tzinfo=timezone.utc),
        fetched_at=_NOW,
        stale=False,
    )
    return [*_readings(), codex]


class _FakeFetcher:
    def __init__(self, readings: list[Reading]) -> None:
        self._readings = readings

    def get_latest_readings(self) -> list[Reading]:
        return self._readings


class _FakeBacklight:
    """Stands in for the sysfs backlight so a double-tap can engage manual sleep
    in tests (the real Backlight reports unavailable with no hardware device)."""

    def __init__(
        self, available: bool = True, level: int = 15, max_level: int = 31,
    ) -> None:
        self.available = available
        self.max_level = max_level
        self.power: list[bool] = []
        self.levels: list[int] = []  # set_level history
        self._level = level

    @property
    def current_level(self) -> int:
        return self._level

    def set_power(self, on: bool) -> None:
        self.power.append(on)

    def set_level(self, level: int) -> None:
        self._level = level
        self.levels.append(level)


class _RecordingSurface:
    """Delegate to a real pygame surface while recording text blits."""

    def __init__(self, surface) -> None:
        self.surface = surface
        self.blit_rects: list[pygame.Rect] = []

    def blit(self, source, dest):
        self.blit_rects.append(source.get_rect(topleft=dest))
        return self.surface.blit(source, dest)


@pytest.fixture
def gui():
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_mode((480, 320))
    fetcher = _FakeFetcher(_readings())
    yield DashboardGui(fetcher, (480, 320))  # type: ignore[arg-type]
    pygame.display.quit()


def test_draw_main_does_not_raise(gui) -> None:
    layout = build_main_layout(_readings(), (480, 320))
    gui._draw_main(layout)


def test_draw_detail_does_not_raise(gui) -> None:
    gui._state = ViewState(detail_provider=Provider.CLAUDE)
    gui._draw_detail(_readings())


def test_draw_detail_quotaless_provider(gui) -> None:
    gui._state = ViewState(detail_provider=Provider.OLLAMA)
    gui._draw_detail(_readings())


def test_quotaless_tile_paints_its_detail_line(gui) -> None:
    """Regression: a quota-less tile used to render as a bare title because
    ``_draw_tile`` never drew ``tile.detail``. The detail text must paint
    pixels in the tile body, not leave it empty."""
    from usage_dashboard.client.gui import _TILE_BG

    readings = [
        Reading(
            provider=Provider.OLLAMA,
            status=ReadingStatus.CURRENT,
            session_percent=None,
            session_resets_at=None,
            weekly_percent=None,
            weekly_resets_at=None,
            fetched_at=_NOW,
            stale=False,
            detail="week req 9 tok 2M",
        )
    ]
    layout = build_main_layout(readings, (480, 320))
    gui._screen.fill((0, 0, 0))
    gui._draw_main(layout)

    tile = layout.tiles[0]
    assert tile.bars == [] and tile.detail is not None
    # Sample the body of the tile (below the title band): at least one pixel
    # must differ from the tile background, i.e. the detail text painted.
    body = pygame.Rect(
        tile.rect.x + 4,
        tile.rect.y + tile.rect.h // 3,
        tile.rect.w - 8,
        tile.rect.h // 2,
    )
    painted = False
    for x in range(body.x, body.x + body.w, 3):
        for y in range(body.y, body.y + body.h, 3):
            if gui._screen.get_at((x, y))[:3] != tuple(_TILE_BG):
                painted = True
                break
        if painted:
            break
    assert painted


def test_detail_for_absent_provider_falls_back_to_main(gui) -> None:
    gui._state = ViewState(detail_provider=Provider.CLAUDE)
    gui._draw_detail([])  # provider not in readings
    assert gui._state.detail_provider is None


def test_bars_align_within_a_column() -> None:
    # With the paired-row layout, full-width tiles (Claude, Codex) form one
    # column and must share an identical bar track so they read as aligned rows;
    # the half-width paired tiles (z.ai, ollama) can't share that track but must
    # match each other in track *length*. Worst case: Claude folds in a wide
    # "work" account and z.ai sits at 100% while others are low.
    pygame.display.init()
    pygame.font.init()
    size = (1280, 720)  # the units run the 5" panel rotated to landscape
    pygame.display.set_mode(size)
    try:
        def r(provider: Provider, sp: float, wp: float) -> Reading:
            return Reading(
                provider=provider, status=ReadingStatus.CURRENT,
                session_percent=sp, session_resets_at=_NOW.replace(tzinfo=timezone.utc),
                weekly_percent=wp, weekly_resets_at=_NOW.replace(tzinfo=timezone.utc),
                fetched_at=_NOW, stale=False,
            )
        readings = [
            r(Provider.CLAUDE, 9, 9), r(Provider.CLAUDE_WORK, 100, 100),
            r(Provider.CODEX, 50, 50),
            r(Provider.ZAI, 100, 100), r(Provider.OLLAMA, 7, 5),
        ]
        gui = DashboardGui(_FakeFetcher(readings), size)  # type: ignore[arg-type]
        layout = build_main_layout(
            readings, size, tile_overhead=gui._tile_overhead,
        )
        label_cols = gui._label_col_widths(layout.tiles)

        # Side-by-side tiles of the same size (z.ai | ollama) must share an
        # identical track so their bars line up; every tile's track must stay
        # drawable.
        from collections import defaultdict

        groups: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
        for t in layout.tiles:
            tx, tr = gui._bar_track(t.rect, label_cols[t.compact], compact=t.compact)
            assert tr - tx > 20, (t.provider, tx, tr)  # track stays drawable
            # Track position relative to the tile's own left edge — side-by-side
            # tiles differ in absolute x but must match relative to their tile.
            groups[(t.rect.w, t.rect.h)].append((tx - t.rect.x, tr - t.rect.x))

        paired = [rels for rels in groups.values() if len(rels) > 1]
        assert paired, "expected a paired same-size group (z.ai | ollama)"
        for rels in paired:
            assert len(set(rels)) == 1, rels
    finally:
        pygame.display.quit()


def _bottom_pad(tile, gui) -> int:
    """Distance from the last bar's bottom edge to the tile's bottom inner
    edge, computed with the same geometry as ``_draw_tile``."""
    pad = gui._tile_pad
    title_h = gui._font_title.get_linesize()
    content_top = tile.rect.y + pad + title_h + pad // 2
    bottom = tile.rect.y + tile.rect.h - pad
    n = max(len(tile.bars), 1)
    row_h = (bottom - content_top) // n
    bar_h = max(8, row_h // 3)
    cy_last = content_top + (n - 1) * row_h + row_h // 2
    bar_bottom = cy_last + bar_h // 2
    return bottom - bar_bottom


def test_equal_bottom_padding_across_bar_counts() -> None:
    # The 2-bar tile (Codex) and 3-bar tile (Claude+Fable) must have equal
    # padding from the last bar to the bottom of their cells.  Before the
    # overhead-aware row distribution, the 3-bar tile got excess bottom padding
    # and the 2-bar tile's bars were squished to minimum height.
    pygame.display.init()
    pygame.font.init()
    size = (1280, 720)
    pygame.display.set_mode(size)
    try:
        reset = _NOW.replace(tzinfo=timezone.utc) + timedelta(days=3)
        claude = Reading(
            provider=Provider.CLAUDE, status=ReadingStatus.CURRENT,
            session_percent=50.0, session_resets_at=reset,
            weekly_percent=90.0, weekly_resets_at=reset,
            fetched_at=_NOW, stale=False,
            scoped_limits=[ScopedLimit(
                name="Fable", percent=13.0, resets_at=reset, is_active=False,
            )],
        )
        codex = Reading(
            provider=Provider.CODEX, status=ReadingStatus.CURRENT,
            session_percent=60.0, session_resets_at=reset,
            weekly_percent=40.0, weekly_resets_at=reset,
            fetched_at=_NOW, stale=False,
        )
        readings = [claude, codex]
        gui = DashboardGui(_FakeFetcher(readings), size)  # type: ignore[arg-type]
        layout = build_main_layout(
            readings, size, tile_overhead=gui._tile_overhead,
        )
        by = {t.provider: t for t in layout.tiles}
        claude_tile = by[Provider.CLAUDE]
        codex_tile = by[Provider.CODEX]
        assert len(claude_tile.bars) == 3  # Session, Weekly, Fable
        assert len(codex_tile.bars) == 2

        pad_claude = _bottom_pad(claude_tile, gui)
        pad_codex = _bottom_pad(codex_tile, gui)
        assert abs(pad_claude - pad_codex) <= 1, (
            f"bottom padding differs: Claude={pad_claude}, Codex={pad_codex}"
        )

        # The 2-bar tile's bars must not be squished to minimum height —
        # the row height should match the 3-bar tile's (within 1px for
        # integer division).
        def row_h(tile) -> int:
            pad = gui._tile_pad
            t_h = gui._font_title.get_linesize()
            ct = tile.rect.y + pad + t_h + pad // 2
            bt = tile.rect.y + tile.rect.h - pad
            return (bt - ct) // max(len(tile.bars), 1)

        assert abs(row_h(claude_tile) - row_h(codex_tile)) <= 1, (
            f"row heights differ: Claude={row_h(claude_tile)}, "
            f"Codex={row_h(codex_tile)}"
        )
    finally:
        pygame.display.quit()


def test_smaller_resolution_renders(gui) -> None:
    layout = build_main_layout(_readings(), (240, 320))
    gui._width, gui._height = 240, 320
    gui._draw_main(layout)


def test_responsive_main_detail_and_overlay_render_at_audit_sizes() -> None:
    """Exercise real pygame paths for four providers at tiny and normal sizes.

    The 240px cases use a summary tile rather than trying to squeeze reset
    columns beside 16px glyphs. Long detail/diagnostic values still exercise
    the truncation paths without weakening the readable font floors.
    """
    long_detail = Reading(
        provider=Provider.OLLAMA,
        status=ReadingStatus.CURRENT,
        session_percent=50.0,
        session_resets_at=_NOW.replace(tzinfo=timezone.utc),
        weekly_percent=90.0,
        weekly_resets_at=_NOW.replace(tzinfo=timezone.utc),
        fetched_at=_NOW,
        stale=False,
        detail="week req 123456789 tok 987654321.0M",
    )
    diag_snapshot = diagnostics.Diagnostics(
        hostname="usage-dashboard-pi-with-a-long-hostname",
        addresses=["192.168.100.123", "10.0.0.123"],
        server_host="usage-dashboard.example.internal",
        running_commit="abcdef123456",
        check=diagnostics.UpdateCheck(
            when=_NOW.replace(tzinfo=timezone.utc),
            result="pip-failed",
            commit="abcdef123456",
        ),
        change=None,
    )

    for size in _AUDIT_SIZES:
        pygame.display.init()
        pygame.font.init()
        screen = pygame.display.set_mode(size)
        try:
            readings = _four_provider_readings()
            gui = DashboardGui(_FakeFetcher(readings), size)  # type: ignore[arg-type]
            layout = build_main_layout(
                readings, size, tile_overhead=gui._tile_overhead,
            )
            assert [tile.provider for tile in layout.tiles] == [
                Provider.CLAUDE, Provider.CODEX, Provider.ZAI, Provider.OLLAMA,
            ]
            screen.fill((0, 0, 0))
            gui._draw_main(layout)

            # Every actual bar track has positive room after the responsive
            # label/reset allocation, and all geometry stays on the screen.
            label_cols = gui._label_col_widths(layout.tiles)
            for i, first in enumerate(layout.tiles):
                for second in layout.tiles[i + 1:]:
                    assert not (
                        first.rect.x < second.rect.x + second.rect.w
                        and second.rect.x < first.rect.x + first.rect.w
                        and first.rect.y < second.rect.y + second.rect.h
                        and second.rect.y < first.rect.y + first.rect.h
                    )
            for tile in layout.tiles:
                assert tile.rect.x >= 0 and tile.rect.y >= 0
                assert tile.rect.x + tile.rect.w <= size[0]
                assert tile.rect.y + tile.rect.h <= layout.status_rect.y
                if tile.ultra_compact:
                    specs = gui._ultra_compact_text_specs(tile)
                    rects = [spec[4] for spec in specs]
                    for first_index, first in enumerate(rects):
                        assert first.x >= tile.rect.x
                        assert first.y >= tile.rect.y
                        assert first.x + first.w <= tile.rect.x + tile.rect.w
                        assert first.y + first.h <= tile.rect.y + tile.rect.h
                        for second in rects[first_index + 1:]:
                            overlap_x = first.x < second.x + second.w
                            overlap_y = first.y < second.y + second.h
                            overlap_x = overlap_x and second.x < first.x + first.w
                            overlap_y = overlap_y and second.y < first.y + first.h
                            assert not (overlap_x and overlap_y)
                    # Each quota-bearing tile keeps both aggregate percentages;
                    # the quota-less tile keeps a one-line detail placeholder.
                    summary_specs = [
                        spec for spec in specs if spec[0].startswith("summary-")
                    ]
                    if tile.bars:
                        assert len(summary_specs) == min(2, len(tile.bars))
                        assert all(
                            bar.percent_text in spec[2]
                            for bar, spec in zip(tile.bars[:2], summary_specs)
                        )
                    else:
                        assert summary_specs == []
                    if tile.provider is Provider.ZAI:
                        assert tile.status_marker == "!"
                        assert any(spec[0] == "status" for spec in specs)
                    assert hit_test(
                        layout,
                        (tile.rect.x + tile.rect.w // 2,
                         tile.rect.y + tile.rect.h // 2),
                    ) is tile.provider
                elif tile.bars:
                    track_x, track_right = gui._bar_track(
                        tile.rect, label_cols[tile.compact], tile.compact,
                    )
                    assert track_right - track_x > 20
                    bar_font = gui._bar_font(tile.compact)
                    assert all(
                        bar_font.size(
                            gui._fit_text(
                                bar_font, gui._bar_label(bar), label_cols[tile.compact]
                            )
                        )[0] <= label_cols[tile.compact]
                        for bar in tile.bars
                    )
            refresh = layout.refresh_rect
            assert refresh is not None
            assert refresh.w >= MIN_TOUCH_TARGET
            assert refresh.h >= MIN_TOUCH_TARGET
            assert layout.status_rect.contains(refresh.x, refresh.y)
            assert layout.status_rect.contains(
                refresh.x + refresh.w - 1, refresh.y + refresh.h - 1
            )

            # Check the metrics pygame actually renders, not just the nominal
            # point sizes passed to Font().  These floors keep the smallest
            # audit view legible while the renderer truncates to its columns.
            for font, line_floor, glyph_floor in (
                (gui._font_small, 14, 13),
                (gui._font, 16, 15),
                (gui._font_title, 20, 19),
            ):
                assert font.get_height() >= line_floor
                glyph = font.render("Ag", True, (255, 255, 255))
                assert glyph.get_bounding_rect().height >= glyph_floor

            # Detail values use the same surface and font metrics rather than a
            # mock renderer.  The long value forces the narrow two-line path.
            gui._state = ViewState(detail_provider=Provider.OLLAMA)
            detail_readings = [
                reading for reading in readings
                if reading.provider is not Provider.OLLAMA
            ] + [long_detail]
            recorder = _RecordingSurface(screen)
            gui._screen = recorder  # type: ignore[assignment]
            gui._draw_detail(detail_readings)
            gui._screen = screen
            for first_index, first in enumerate(recorder.blit_rects):
                assert first.x >= 0 and first.y >= 0
                assert first.right <= size[0] and first.bottom <= size[1]
                for second in recorder.blit_rects[first_index + 1:]:
                    assert not first.colliderect(second)
            assert gui._state.detail_provider is Provider.OLLAMA

            gui._diag = diag_snapshot
            gui._state = ViewState(overlay=True)
            gui._draw_status_overlay()
            overlay = build_status_overlay(size)
            assert overlay.panel.x >= 0 and overlay.panel.y >= 0
            assert overlay.panel.x + overlay.panel.w <= size[0]
            assert overlay.panel.y + overlay.panel.h <= size[1]
            assert (
                overlay.diag_rect.x + overlay.diag_rect.w
                <= overlay.brightness.region.x
            )
            for control in (
                overlay.brightness.minus,
                overlay.brightness.plus,
                overlay.brightness.level_rect,
            ):
                assert overlay.brightness.region.contains(control.x, control.y)
                assert overlay.brightness.region.contains(
                    control.x + control.w - 1, control.y + control.h - 1
                )
            for control in (overlay.brightness.minus, overlay.brightness.plus):
                assert control.w >= MIN_TOUCH_TARGET
                assert control.h >= MIN_TOUCH_TARGET
            minus = overlay.brightness.minus
            plus = overlay.brightness.plus
            level = overlay.brightness.level_rect
            assert not (
                minus.x < plus.x + plus.w
                and plus.x < minus.x + minus.w
                and minus.y < plus.y + plus.h
                and plus.y < minus.y + minus.h
            )
            assert not (
                level.x < minus.x + minus.w
                and minus.x < level.x + level.w
                and level.y < minus.y + minus.h
                and minus.y < level.y + level.h
            )
            assert not (
                level.x < plus.x + plus.w
                and plus.x < level.x + level.w
                and level.y < plus.y + plus.h
                and plus.y < level.y + level.h
            )
        finally:
            pygame.display.quit()


def test_compact_bar_track_wider_than_full_width(gui) -> None:
    from usage_dashboard.client.layout import Rect

    r = Rect(0, 0, 400, 200)
    full_label = gui._font.size("Session 100%")[0]
    compact_label = gui._font.size("S 100%")[0]
    tx_full, tr_full = gui._bar_track(r, full_label, compact=False)
    tx_comp, tr_comp = gui._bar_track(r, compact_label, compact=True)
    assert (tr_comp - tx_comp) > (tr_full - tx_full)


def test_finger_tap_routes_through_touch_rotation() -> None:
    # On a 1280x720 landscape panel rotated 90°, a finger reported in the
    # panel's portrait frame must resolve to the tile under it.
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_mode((1280, 720))
    try:
        size = (1280, 720)
        gui = DashboardGui(_FakeFetcher(_readings()), size, touch_rotate=90)  # type: ignore[arg-type]
        layout = build_main_layout(_readings(), size)
        ollama = next(t for t in layout.tiles if t.provider is Provider.OLLAMA)
        sx = (ollama.rect.x + ollama.rect.w / 2) / size[0]
        sy = (ollama.rect.y + ollama.rect.h / 2) / size[1]
        # Inverse-rotate to the device (portrait) frame the panel would report.
        from usage_dashboard.client.layout import hit_test, rotate_touch_norm
        dx, dy = rotate_touch_norm(sx, sy, 270)
        event = pygame.event.Event(
            pygame.FINGERDOWN, {"x": dx, "y": dy, "touch_id": 0, "finger_id": 0}
        )
        pos = gui._tap_position(event)
        assert pos is not None
        assert hit_test(layout, pos) is Provider.OLLAMA
    finally:
        pygame.display.quit()


def test_mouse_tap_is_not_rotated() -> None:
    # Dev/windowed mouse events are already in screen pixels; rotation must not
    # touch them even when touch_rotate is set.
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_mode((1280, 720))
    try:
        gui = DashboardGui(_FakeFetcher(_readings()), (1280, 720), touch_rotate=90)  # type: ignore[arg-type]
        event = pygame.event.Event(pygame.MOUSEBUTTONDOWN, {"pos": (300, 200), "button": 1})
        assert gui._tap_position(event) == (300, 200)
    finally:
        pygame.display.quit()


# -- double-tap-to-sleep ----------------------------------------------------


def test_double_tap_detector_pairs_quick_close_taps() -> None:
    d = DoubleTapDetector(window_ms=350, tolerance_px=50)
    assert d.register(1000, (10, 10)) is False  # first tap never pairs
    assert d.register(1200, (12, 12)) is True   # within 350ms and 50px


def test_double_tap_detector_rejects_slow_second_tap() -> None:
    d = DoubleTapDetector(window_ms=350, tolerance_px=50)
    assert d.register(1000, (10, 10)) is False
    assert d.register(1500, (10, 10)) is False  # 500ms apart, too slow


def test_double_tap_detector_rejects_distant_second_tap() -> None:
    d = DoubleTapDetector(window_ms=350, tolerance_px=20)
    assert d.register(1000, (10, 10)) is False
    assert d.register(1100, (200, 200)) is False  # quick but far apart


def test_double_tap_detector_does_not_chain_triples() -> None:
    d = DoubleTapDetector(window_ms=350, tolerance_px=50)
    assert d.register(1000, (10, 10)) is False
    assert d.register(1100, (10, 10)) is True   # pair fires, state consumed
    assert d.register(1150, (10, 10)) is False  # third starts a fresh pair
    assert d.register(1200, (10, 10)) is True   # fourth completes it


def _post_taps(positions: list[tuple[int, int]]) -> None:
    pygame.event.clear()
    for pos in positions:
        pygame.event.post(
            pygame.event.Event(pygame.MOUSEBUTTONDOWN, {"pos": pos, "button": 1})
        )


def test_double_tap_engages_manual_sleep_and_returns_home() -> None:
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_mode((480, 320))
    try:
        backlight = _FakeBacklight(available=True)
        gui = DashboardGui(
            _FakeFetcher(_readings()), (480, 320), backlight=backlight,  # type: ignore[arg-type]
        )
        gui._state = ViewState(detail_provider=Provider.CLAUDE)  # a detail view is open
        layout = build_main_layout(_readings(), (480, 320))
        _post_taps([(200, 150), (200, 150)])
        gui._handle_events(layout, swallow_wake=False, now=_NOW)
        assert gui._manual_sleep is True
        assert gui._state.detail_provider is None  # reset to the home grid
        assert gui._is_dark(_NOW) is True
    finally:
        pygame.display.quit()


def test_double_tap_ignored_when_backlight_unavailable() -> None:
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_mode((480, 320))
    try:
        gui = DashboardGui(
            _FakeFetcher(_readings()), (480, 320),
            backlight=_FakeBacklight(available=False),  # type: ignore[arg-type]
        )
        layout = build_main_layout(_readings(), (480, 320))
        _post_taps([(200, 150), (200, 150)])
        gui._handle_events(layout, swallow_wake=False, now=_NOW)
        assert gui._manual_sleep is False
        assert gui._is_dark(_NOW) is False
    finally:
        pygame.display.quit()


# -- brightness overlay -----------------------------------------------------


def _status_pos(size: tuple[int, int]) -> tuple[int, int]:
    """A point inside the status ("Updated…") line for *size*."""
    layout = build_main_layout(_readings(), size)
    sr = layout.status_rect
    return sr.x + sr.w // 2, sr.y + sr.h // 2


def _make_brightness_gui(
    size: tuple[int, int] = (480, 320),
    *,
    available: bool = True,
    level: int = 15,
    state_file=None,
    steps: int = 10,
    status_dir=None,
):
    return DashboardGui(
        _FakeFetcher(_readings()), size,  # type: ignore[arg-type]
        backlight=_FakeBacklight(available=available, level=level),  # type: ignore[arg-type]
        brightness_steps=steps,
        brightness_state_file=state_file,
        status_dir=status_dir,
    )


def _plus(size: tuple[int, int]) -> tuple[int, int]:
    b = build_status_overlay(size).brightness
    return b.plus.x + b.plus.w // 2, b.plus.y + b.plus.h // 2


def _minus(size: tuple[int, int]) -> tuple[int, int]:
    b = build_status_overlay(size).brightness
    return b.minus.x + b.minus.w // 2, b.minus.y + b.minus.h // 2


def test_status_tap_opens_overlay() -> None:
    pygame.display.init()
    pygame.font.init()
    size = (480, 320)
    pygame.display.set_mode(size)
    try:
        gui = _make_brightness_gui(size)
        layout = build_main_layout(_readings(), size)
        _post_taps([_status_pos(size)])
        gui._handle_events(layout, swallow_wake=False, now=_NOW)
        assert gui._state.overlay is True
        assert gui._diag is not None  # diagnostics gathered on open
    finally:
        pygame.display.quit()


def test_status_tap_opens_overlay_even_without_backlight() -> None:
    # Diagnostics are useful with no controllable backlight, so the overlay must
    # still open (unlike double-tap-to-sleep); the +/- side just no-ops.
    pygame.display.init()
    pygame.font.init()
    size = (480, 320)
    pygame.display.set_mode(size)
    try:
        gui = _make_brightness_gui(size, available=False)
        layout = build_main_layout(_readings(), size)
        _post_taps([_status_pos(size)])
        gui._handle_events(layout, swallow_wake=False, now=_NOW)
        assert gui._state.overlay is True
        _post_taps([_plus(size)])
        gui._handle_events(layout, swallow_wake=False, now=_NOW)
        assert gui._backlight.levels == []  # type: ignore[attr-defined]  # inert
    finally:
        pygame.display.quit()


def test_plus_minus_nudge_changes_level_and_persists(tmp_path) -> None:
    pygame.display.init()
    pygame.font.init()
    size = (480, 320)
    pygame.display.set_mode(size)
    try:
        from usage_dashboard.client.brightness import load_level

        state = tmp_path / "brightness"
        # Start mid-scale (step 5 of 10 on a 31-max panel) so + and - both move.
        gui = _make_brightness_gui(size, level=16, state_file=state, steps=10)
        gui._state = ViewState(overlay=True)
        before = gui._brightness_step

        _post_taps([_plus(size)])
        gui._handle_events(build_main_layout(_readings(), size),
                           swallow_wake=False, now=_NOW)
        assert gui._brightness_step == before + 1
        assert gui._backlight.levels  # set_level was called  # type: ignore[attr-defined]
        assert load_level(state) == gui._backlight.levels[-1]  # type: ignore[attr-defined]

        _post_taps([_minus(size)])
        gui._handle_events(build_main_layout(_readings(), size),
                           swallow_wake=False, now=_NOW)
        assert gui._brightness_step == before
    finally:
        pygame.display.quit()


def test_tap_outside_panel_closes_overlay() -> None:
    pygame.display.init()
    pygame.font.init()
    size = (480, 320)
    pygame.display.set_mode(size)
    try:
        gui = _make_brightness_gui(size)
        gui._state = ViewState(overlay=True)
        _post_taps([(1, 1)])  # corner, outside the centred card
        gui._handle_events(build_main_layout(_readings(), size),
                           swallow_wake=False, now=_NOW)
        assert gui._state.overlay is False
    finally:
        pygame.display.quit()


def test_diagnostics_column_tap_does_not_close() -> None:
    # A tap on the left (diagnostics) column is display-only — it must not be
    # read as "tap outside to close".
    pygame.display.init()
    pygame.font.init()
    size = (480, 320)
    pygame.display.set_mode(size)
    try:
        gui = _make_brightness_gui(size)
        gui._state = ViewState(overlay=True)
        dr = build_status_overlay(size).diag_rect
        _post_taps([(dr.x + dr.w // 2, dr.y + dr.h // 2)])
        gui._handle_events(build_main_layout(_readings(), size),
                           swallow_wake=False, now=_NOW)
        assert gui._state.overlay is True
    finally:
        pygame.display.quit()


def test_nudge_clamps_at_rails() -> None:
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_mode((480, 320))
    try:
        gui = _make_brightness_gui((480, 320), level=31, steps=10)  # at max
        assert gui._brightness_step == 10
        gui._nudge_brightness(+1)
        assert gui._brightness_step == 10  # can't exceed the top rail
        for _ in range(20):
            gui._nudge_brightness(-1)
        assert gui._brightness_step == 1  # floors at 1, never 0
    finally:
        pygame.display.quit()


def test_persisted_level_applied_at_startup(tmp_path) -> None:
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_mode((480, 320))
    try:
        state = tmp_path / "brightness"
        state.write_text("8")  # a level persisted from a previous run
        gui = _make_brightness_gui((480, 320), level=31, state_file=state, steps=10)
        # Startup re-applies the persisted level to the panel (reboot survival)...
        assert gui._backlight.levels == [8]  # type: ignore[attr-defined]
        # ...and the step reflects it, not the hardware's power-on default.
        assert gui._brightness_step == step_for_level(8, 10, 31)
    finally:
        pygame.display.quit()


def test_draw_status_overlay_does_not_raise(tmp_path) -> None:
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_mode((1280, 720))
    try:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "update-last-check").write_text(
            "2026-06-26T01:00:00Z up-to-date a1b2c3d4"
        )
        gui = _make_brightness_gui(  # odd step count + a real diag snapshot
            (1280, 720), steps=11, status_dir=state_dir,
        )
        gui._diag = diagnostics.gather(state_dir, "http://server.example:8080")
        gui._draw_status_overlay()
    finally:
        pygame.display.quit()


def test_tap_wakes_from_manual_sleep() -> None:
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_mode((480, 320))
    try:
        gui = DashboardGui(
            _FakeFetcher(_readings()), (480, 320),
            backlight=_FakeBacklight(available=True),  # type: ignore[arg-type]
        )
        gui._manual_sleep = True
        assert gui._is_dark(_NOW) is True
        layout = build_main_layout(_readings(), (480, 320))
        _post_taps([(200, 150)])
        gui._handle_events(layout, swallow_wake=True, now=_NOW)
        assert gui._manual_sleep is False
        assert gui._is_dark(_NOW) is False  # no schedule -> awake again
    finally:
        pygame.display.quit()


# -- on-demand refresh (WI-012) ---------------------------------------------


class _RefreshFetcher:
    """_FakeFetcher plus a controllable request_refresh: the test holds the
    thread open (via ``release``) so it can assert the in-flight state, then
    lets it finish."""

    def __init__(self, readings: list[Reading]) -> None:
        self._readings = readings
        self.refreshes = 0
        self.release = threading.Event()
        self.fail = False

    def get_latest_readings(self) -> list[Reading]:
        return self._readings

    def request_refresh(self) -> bool:
        self.refreshes += 1
        self.release.wait(timeout=5)
        return not self.fail


def _refresh_pos(size: tuple[int, int]) -> tuple[int, int]:
    """Centre of the on-demand refresh button for *size*."""
    r = build_main_layout(_readings(), size).refresh_rect
    assert r is not None
    return r.x + r.w // 2, r.y + r.h // 2


def test_refresh_button_tap_requests_refresh() -> None:
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_mode((480, 320))
    try:
        fetcher = _RefreshFetcher(_readings())
        gui = DashboardGui(fetcher, (480, 320))  # type: ignore[arg-type]
        layout = build_main_layout(_readings(), (480, 320))
        _post_taps([_refresh_pos((480, 320))])
        gui._handle_events(layout, swallow_wake=False, now=_NOW)
        # The tap landed on the refresh button, not a tile or the overlay.
        assert fetcher.refreshes == 1
        assert gui._refresh_pending is True
        assert gui._refresh_feedback == "refreshing\u2026"
        assert gui._state.overlay is False
        assert gui._state.detail_provider is None
        # Let the request finish: feedback resolves, button unhighlights.
        fetcher.release.set()
        thread = gui._refresh_thread
        assert thread is not None
        thread.join(timeout=5)
        assert gui._refresh_pending is False
        assert gui._refresh_feedback == "refreshed"
    finally:
        pygame.display.quit()


def test_refresh_button_failure_feedback() -> None:
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_mode((480, 320))
    try:
        fetcher = _RefreshFetcher(_readings())
        fetcher.fail = True
        gui = DashboardGui(fetcher, (480, 320))  # type: ignore[arg-type]
        layout = build_main_layout(_readings(), (480, 320))
        _post_taps([_refresh_pos((480, 320))])
        gui._handle_events(layout, swallow_wake=False, now=_NOW)
        fetcher.release.set()
        thread = gui._refresh_thread
        assert thread is not None
        thread.join(timeout=5)
        assert gui._refresh_feedback == "refresh failed"
    finally:
        pygame.display.quit()


def test_start_refresh_ignored_while_pending() -> None:
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_mode((480, 320))
    try:
        fetcher = _RefreshFetcher(_readings())
        gui = DashboardGui(fetcher, (480, 320))  # type: ignore[arg-type]
        gui._start_refresh()
        first = gui._refresh_thread
        gui._start_refresh()  # already in flight: must not spawn a second
        assert gui._refresh_thread is first
        assert fetcher.refreshes == 1
        fetcher.release.set()
        assert first is not None
        first.join(timeout=5)
        assert gui._refresh_pending is False
    finally:
        pygame.display.quit()


def test_refresh_button_does_not_steal_status_tap() -> None:
    # The refresh button occupies the right edge of the status bar; a tap on
    # the rest of the bar must still open the diagnostics overlay.
    pygame.display.init()
    pygame.font.init()
    size = (480, 320)
    pygame.display.set_mode(size)
    try:
        fetcher = _RefreshFetcher(_readings())
        gui = DashboardGui(fetcher, size)  # type: ignore[arg-type]
        layout = build_main_layout(_readings(), size)
        sr = layout.status_rect
        r = layout.refresh_rect
        assert r is not None
        # A point on the status bar far from the button (centre-left).
        tap = (sr.x + sr.w // 3, sr.y + sr.h // 2)
        assert not r.contains(*tap)
        _post_taps([tap])
        gui._handle_events(layout, swallow_wake=False, now=_NOW)
        assert gui._state.overlay is True
        assert fetcher.refreshes == 0
    finally:
        pygame.display.quit()


def _is_tofu(font: "pygame.font.Font", ch: str) -> bool:
    """True when *ch* renders as the font's .notdef box.

    pygame reports metrics for missing glyphs rather than None, so the only
    reliable detector is comparing against a codepoint the font certainly
    lacks — U+E123 sits in the Private Use Area.
    """
    notdef = font.metrics("")
    return font.metrics(ch) == notdef


def test_ui_text_glyphs_exist_in_the_bundled_font() -> None:
    """Every non-ASCII character the GUI renders as *text* must be drawable.

    The refresh button originally used U+27F3 and shipped a tofu box to the
    panel: pygame's bundled freesansbold.ttf has no rotation arrow, and the
    metrics-based checks people reach for return .notdef rather than None.
    The icon is drawn with primitives now; this guards the text that isn't.
    """
    pygame.font.init()
    font = pygame.font.Font(None, 40)
    # Sanity-check the detector itself against a glyph the font really lacks.
    assert _is_tofu(font, "⟳"), "detector failed: U+27F3 should be missing"
    for ch in ("·", "…", "-", "+"):  # middot, ellipsis, brightness +/-
        assert not _is_tofu(font, ch), f"{ch!r} renders as tofu in the default font"


def test_refresh_icon_draws_inside_the_button() -> None:
    # The icon is drawn, not rendered from a font, so guard that it actually
    # puts pixels down and keeps them within the button's bounds.
    pygame.display.init()
    pygame.font.init()
    size = (1280, 720)
    screen = pygame.display.set_mode(size)
    try:
        fetcher = _RefreshFetcher(_readings())
        gui = DashboardGui(fetcher, size)  # type: ignore[arg-type]
        layout = build_main_layout(_readings(), size)
        rect = layout.refresh_rect
        assert rect is not None
        screen.fill((0, 0, 0))
        gui._draw_refresh_icon(rect, (255, 255, 255))
        area = pygame.Rect(rect.x, rect.y, rect.w, rect.h)
        # get_bounding_rect only reports drawn content when a colorkey marks
        # the background; without one it returns the whole surface.
        snapshot = screen.copy()
        snapshot.set_colorkey((0, 0, 0))
        drawn = snapshot.get_bounding_rect()
        assert drawn.width > 0, "icon drew nothing"
        assert area.contains(drawn), f"icon drew outside the button: {drawn} vs {area}"
    finally:
        pygame.display.quit()


def test_refresh_feedback_expires() -> None:
    # The first cut set _refresh_feedback in four places and rendered it in
    # none — dead state, so a tap reported nothing in words. Guard both that
    # it is readable and that it doesn't stick around forever.
    pygame.display.init()
    pygame.font.init()
    size = (480, 320)
    pygame.display.set_mode(size)
    try:
        gui = DashboardGui(_RefreshFetcher(_readings()), size)  # type: ignore[arg-type]
        gui._set_refresh_feedback("refreshed", hold_ms=10_000)
        assert gui._current_refresh_feedback() == "refreshed"
        # Expire it by moving the deadline into the past.
        gui._refresh_feedback_until = pygame.time.get_ticks() - 1
        assert gui._current_refresh_feedback() == ""
        assert gui._refresh_feedback == ""  # cleared, not just hidden
    finally:
        pygame.display.quit()
