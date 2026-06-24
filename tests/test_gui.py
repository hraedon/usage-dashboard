"""Headless smoke tests for the pygame GUI.

These run the real draw path under SDL's dummy video driver (no display), so a
broken blit/geometry call is caught in CI without hardware. Skipped entirely
when the optional ``gui`` extra (pygame) isn't installed.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

pygame = pytest.importorskip("pygame")

from usage_dashboard.client.gui import (  # noqa: E402
    DashboardGui,
    DoubleTapDetector,
)
from usage_dashboard.client.layout import ViewState, build_main_layout  # noqa: E402
from usage_dashboard.shared.models import (  # noqa: E402
    Provider,
    Reading,
    ReadingStatus,
)

_NOW = datetime(2026, 1, 10, 12, 0, 0)


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
        r(Provider.UMANS, session_percent=None, weekly_percent=None,
          session_resets_at=None, weekly_resets_at=None, detail="req 5 tok 1M"),
    ]


class _FakeFetcher:
    def __init__(self, readings: list[Reading]) -> None:
        self._readings = readings

    def get_latest_readings(self) -> list[Reading]:
        return self._readings


class _FakeBacklight:
    """Stands in for the sysfs backlight so a double-tap can engage manual sleep
    in tests (the real Backlight reports unavailable with no hardware device)."""

    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.power: list[bool] = []

    def set_power(self, on: bool) -> None:
        self.power.append(on)


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
    gui._state = ViewState(detail_provider=Provider.UMANS)
    gui._draw_detail(_readings())


def test_detail_for_absent_provider_falls_back_to_main(gui) -> None:
    gui._state = ViewState(detail_provider=Provider.CLAUDE)
    gui._draw_detail([])  # provider not in readings
    assert gui._state.detail_provider is None


def test_bars_share_one_track_across_tiles() -> None:
    # Every provider tile must place its bar track at the same x and run the same
    # length, so the bars read as aligned columns regardless of per-tile label
    # widths (percent digit-count, the folded Claude work account). This is the
    # worst case: Claude folds in a wide "work" account and z.ai sits at 100%
    # while the others are low — exactly when the old per-tile column drifted.
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
            r(Provider.ZAI, 100, 100), r(Provider.OLLAMA, 7, 5),
        ]
        gui = DashboardGui(_FakeFetcher(readings), size)  # type: ignore[arg-type]
        layout = build_main_layout(readings, size)
        assert len(layout.tiles) >= 2  # guard: an empty/1-tile layout would pass vacuously
        label_col_w = gui._label_col_width(layout.tiles)
        tracks = {
            tile.provider: gui._bar_track(tile.rect, label_col_w)
            for tile in layout.tiles
        }
        # Identical (track_x, track_right) — hence identical width — everywhere.
        assert len(set(tracks.values())) == 1, tracks
    finally:
        pygame.display.quit()


def test_smaller_resolution_renders(gui) -> None:
    layout = build_main_layout(_readings(), (240, 320))
    gui._width, gui._height = 240, 320
    gui._draw_main(layout)


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
