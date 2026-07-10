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

from usage_dashboard.client import diagnostics  # noqa: E402
from usage_dashboard.client.brightness import step_for_level  # noqa: E402
from usage_dashboard.client.gui import (  # noqa: E402
    DashboardGui,
    DoubleTapDetector,
)
from usage_dashboard.client.layout import (  # noqa: E402
    ViewState,
    build_main_layout,
    build_status_overlay,
)
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
        layout = build_main_layout(readings, size)
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


def test_smaller_resolution_renders(gui) -> None:
    layout = build_main_layout(_readings(), (240, 320))
    gui._width, gui._height = 240, 320
    gui._draw_main(layout)


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
