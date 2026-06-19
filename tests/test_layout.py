from __future__ import annotations

from datetime import datetime, timedelta, timezone

from usage_dashboard.client import format as fmt
from usage_dashboard.client.layout import (
    ViewState,
    build_detail_layout,
    build_main_layout,
    hit_test,
    rotate_touch_norm,
    tap_transition,
)
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus

_NOW = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
_SIZE = (800, 480)


def _reading(provider: Provider, **over: object) -> Reading:
    base = {
        "provider": provider,
        "status": ReadingStatus.CURRENT,
        "session_percent": 50.0,
        "session_resets_at": _NOW + timedelta(hours=2),
        "weekly_percent": 90.0,
        "weekly_resets_at": _NOW + timedelta(days=2),
        "fetched_at": _NOW.replace(tzinfo=None),
        "stale": False,
    }
    base.update(over)
    return Reading(**base)  # type: ignore[arg-type]


def _all_four() -> list[Reading]:
    return [
        _reading(Provider.CLAUDE),
        _reading(Provider.ZAI),
        _reading(Provider.OLLAMA),
        _reading(
            Provider.UMANS, session_percent=None, weekly_percent=None,
            session_resets_at=None, weekly_resets_at=None, detail="req 5 tok 1M",
        ),
    ]


class TestMainLayout:
    def test_tile_per_provider_in_fixed_order(self) -> None:
        layout = build_main_layout(_all_four(), _SIZE, now=_NOW)
        assert [t.provider for t in layout.tiles] == [
            Provider.CLAUDE, Provider.ZAI, Provider.OLLAMA, Provider.UMANS,
        ]

    def test_tiles_within_bounds_and_nonoverlapping(self) -> None:
        layout = build_main_layout(_all_four(), _SIZE, now=_NOW)
        w, h = _SIZE
        rects = [t.rect for t in layout.tiles]
        for r in rects:
            assert r.x >= 0 and r.y >= 0
            assert r.x + r.w <= w
            assert r.y + r.h <= h - layout.status_rect.h  # above the status bar
        for i, a in enumerate(rects):
            for b in rects[i + 1:]:
                overlap_x = a.x < b.x + b.w and b.x < a.x + a.w
                overlap_y = a.y < b.y + b.h and b.y < a.y + a.h
                assert not (overlap_x and overlap_y)

    def test_bars_have_fraction_and_color(self) -> None:
        layout = build_main_layout(_all_four(), _SIZE, now=_NOW)
        claude = layout.tiles[0]
        session, weekly = claude.bars
        assert session.fraction == 0.5
        assert session.color == fmt.GREEN  # 50%
        assert weekly.fraction == 0.9
        assert weekly.color == fmt.RED     # 90%

    def test_quotaless_provider_shows_detail_not_bars(self) -> None:
        layout = build_main_layout(_all_four(), _SIZE, now=_NOW)
        umans = layout.tiles[3]
        assert umans.bars == []
        assert umans.detail == "req 5 tok 1M"

    def test_accent_is_worst_bar_color(self) -> None:
        layout = build_main_layout(_all_four(), _SIZE, now=_NOW)
        # Claude has a 90% weekly bar -> red accent.
        assert layout.tiles[0].accent == fmt.RED

    def test_status_text_mentions_count(self) -> None:
        layout = build_main_layout(_all_four(), _SIZE, now=_NOW)
        assert "4 providers" in layout.status_text

    def test_empty_readings_safe(self) -> None:
        layout = build_main_layout([], _SIZE, now=_NOW)
        assert layout.tiles == []
        assert "Waiting" in layout.status_text

    def test_stale_suffix_in_title(self) -> None:
        layout = build_main_layout(
            [_reading(Provider.CLAUDE, status=ReadingStatus.STALE, stale=True)],
            _SIZE, now=_NOW,
        )
        assert layout.tiles[0].title == "CLAUDE [stale]"

    def test_portrait_resolution_in_bounds(self) -> None:
        layout = build_main_layout(_all_four(), (720, 1280), now=_NOW)
        for t in layout.tiles:
            assert t.rect.x + t.rect.w <= 720
            assert t.rect.y + t.rect.h <= 1280


class TestHitTest:
    def test_tap_inside_tile_returns_provider(self) -> None:
        layout = build_main_layout(_all_four(), _SIZE, now=_NOW)
        r = layout.tiles[2].rect
        assert hit_test(layout, (r.x + r.w // 2, r.y + r.h // 2)) is Provider.OLLAMA

    def test_tap_in_status_bar_returns_none(self) -> None:
        layout = build_main_layout(_all_four(), _SIZE, now=_NOW)
        sr = layout.status_rect
        assert hit_test(layout, (sr.x + 5, sr.y + 5)) is None


class TestViewTransitions:
    def test_tap_tile_opens_detail(self) -> None:
        layout = build_main_layout(_all_four(), _SIZE, now=_NOW)
        r = layout.tiles[0].rect
        state = tap_transition(ViewState(), layout, (r.x + 5, r.y + 5))
        assert state.detail_provider is Provider.CLAUDE

    def test_tap_outside_tile_stays_on_grid(self) -> None:
        layout = build_main_layout(_all_four(), _SIZE, now=_NOW)
        sr = layout.status_rect
        state = tap_transition(ViewState(), layout, (sr.x + 1, sr.y + 1))
        assert state.detail_provider is None

    def test_tap_in_detail_returns_to_grid(self) -> None:
        layout = build_main_layout(_all_four(), _SIZE, now=_NOW)
        state = ViewState(detail_provider=Provider.ZAI)
        assert tap_transition(state, layout, (0, 0)).detail_provider is None


class TestDetailLayout:
    def test_percent_provider_lines(self) -> None:
        detail = build_detail_layout(_reading(Provider.CLAUDE), now=_NOW)
        labels = [line.label for line in detail.lines]
        assert "Session" in labels
        assert "Weekly" in labels
        assert "Status" in labels
        assert "Fetched" in labels

    def test_quotaless_provider_shows_detail_line(self) -> None:
        umans = _reading(
            Provider.UMANS, session_percent=None, weekly_percent=None,
            session_resets_at=None, weekly_resets_at=None, detail="req 9 tok 2M",
        )
        detail = build_detail_layout(umans, now=_NOW)
        values = [line.value for line in detail.lines]
        assert "req 9 tok 2M" in values


class TestRotateTouchNorm:
    def test_zero_is_identity(self) -> None:
        assert rotate_touch_norm(0.25, 0.75, 0) == (0.25, 0.75)

    def test_360_wraps_to_identity(self) -> None:
        assert rotate_touch_norm(0.25, 0.75, 360) == (0.25, 0.75)

    def test_corners_map_correctly_at_90(self) -> None:
        # Panel top-left (0,0) rotates to screen bottom-left for a 90° CW turn.
        assert rotate_touch_norm(0.0, 0.0, 90) == (0.0, 1.0)
        assert rotate_touch_norm(1.0, 0.0, 90) == (0.0, 0.0)
        assert rotate_touch_norm(0.0, 1.0, 90) == (1.0, 1.0)

    def test_180_inverts_both_axes(self) -> None:
        sx, sy = rotate_touch_norm(0.3, 0.8, 180)
        assert abs(sx - 0.7) < 1e-9
        assert abs(sy - 0.2) < 1e-9

    def test_90_and_270_are_inverses(self) -> None:
        nx, ny = 0.2, 0.6
        sx, sy = rotate_touch_norm(nx, ny, 90)
        back_x, back_y = rotate_touch_norm(sx, sy, 270)
        assert abs(back_x - nx) < 1e-9
        assert abs(back_y - ny) < 1e-9

    def test_tap_lands_on_tile_after_landscape_rotation(self) -> None:
        # A finger over CLAUDE's tile (top-left in a 1280x720 landscape grid)
        # must hit-test to CLAUDE once its portrait-frame touch is rotated.
        size = (1280, 720)
        readings = [_reading(p) for p in (
            Provider.CLAUDE, Provider.ZAI, Provider.OLLAMA, Provider.UMANS
        )]
        layout = build_main_layout(readings, size)
        claude = next(t for t in layout.tiles if t.provider is Provider.CLAUDE)
        # Screen-centre of the CLAUDE tile, normalised to the screen.
        sx = (claude.rect.x + claude.rect.w / 2) / size[0]
        sy = (claude.rect.y + claude.rect.h / 2) / size[1]
        # The panel reports it in portrait frame; inverse of the 90° map.
        device = rotate_touch_norm(sx, sy, 270)
        screen = rotate_touch_norm(device[0], device[1], 90)
        px, py = int(screen[0] * size[0]), int(screen[1] * size[1])
        assert hit_test(layout, (px, py)) is Provider.CLAUDE
