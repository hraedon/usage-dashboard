from __future__ import annotations

from datetime import datetime, timedelta, timezone

from usage_dashboard.client import format as fmt
from usage_dashboard.client.layout import (
    MIN_TOUCH_TARGET,
    ViewState,
    build_detail_layout,
    build_main_layout,
    build_status_overlay,
    hit_test,
    refresh_hit_test,
    rotate_touch_norm,
    tap_transition,
)
from usage_dashboard.shared.models import (
    ModelUsage,
    Provider,
    Reading,
    ReadingStatus,
    ScopedLimit,
)

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


def _all_configured() -> list[Reading]:
    return [
        _reading(Provider.CLAUDE),
        _reading(Provider.ZAI),
        _reading(Provider.OLLAMA),
    ]


class TestMainLayout:
    def test_tile_per_provider_in_fixed_order(self) -> None:
        layout = build_main_layout(_all_configured(), _SIZE, now=_NOW)
        assert [t.provider for t in layout.tiles] == [
            Provider.CLAUDE, Provider.ZAI, Provider.OLLAMA,
        ]

    def test_zai_ollama_share_a_row_claude_full_width(self) -> None:
        layout = build_main_layout(_all_configured(), _SIZE, now=_NOW)
        by = {t.provider: t for t in layout.tiles}
        claude, zai, ollama = by[Provider.CLAUDE], by[Provider.ZAI], by[Provider.OLLAMA]
        # Claude spans a full-width row above the pair.
        assert claude.rect.w > zai.rect.w
        assert claude.rect.y < zai.rect.y
        # zai and ollama share one row: same y, side by side, no overlap.
        assert zai.rect.y == ollama.rect.y
        assert zai.rect.x < ollama.rect.x
        assert zai.rect.x + zai.rect.w <= ollama.rect.x

    def test_codex_after_claude_full_width(self) -> None:
        readings = _all_configured() + [_reading(Provider.CODEX)]
        layout = build_main_layout(readings, _SIZE, now=_NOW)
        order = [t.provider for t in layout.tiles]
        assert order == [
            Provider.CLAUDE, Provider.CODEX, Provider.ZAI, Provider.OLLAMA,
        ]
        by = {t.provider: t for t in layout.tiles}
        # Claude and Codex stack full-width; zai/ollama share the row below them.
        assert by[Provider.CODEX].rect.w == by[Provider.CLAUDE].rect.w
        assert (
            by[Provider.CLAUDE].rect.y
            < by[Provider.CODEX].rect.y
            < by[Provider.ZAI].rect.y
        )

    def test_opencode_is_not_rendered_on_pi(self) -> None:
        """OpenCode remains available to the server and web dashboard only."""
        readings = _all_configured() + [
            _reading(Provider.CODEX), _reading(Provider.OPENCODE),
        ]
        layout = build_main_layout(readings, _SIZE, now=_NOW)
        by = {t.provider: t for t in layout.tiles}
        assert Provider.OPENCODE not in by
        # Codex remains full-width and alone on its row...
        assert by[Provider.CODEX].rect.w == by[Provider.CLAUDE].rect.w
        assert by[Provider.CODEX].compact is False
        assert by[Provider.CODEX].rect.y != by[Provider.ZAI].rect.y
        # ...and the zai/ollama pair is untouched.
        assert by[Provider.ZAI].rect.y == by[Provider.OLLAMA].rect.y

    def test_compact_scoped_limit_label_is_abbreviated(self) -> None:
        """A scoped label on a compact tile shrinks to its initial."""
        ollama = _reading(
            Provider.OLLAMA,
            scoped_limits=[ScopedLimit(
                name="Monthly", percent=9.0,
                resets_at=_NOW + timedelta(days=22), is_active=False,
            )],
        )
        layout = build_main_layout(
            [_reading(Provider.ZAI), ollama], _SIZE, now=_NOW,
        )
        by = {t.provider: t for t in layout.tiles}
        assert by[Provider.OLLAMA].compact is True
        assert [b.label for b in by[Provider.OLLAMA].bars] == ["S", "W", "M"]

    def test_full_width_scoped_limit_label_is_not_abbreviated(self) -> None:
        """Claude is never paired, so its Fable bar keeps the readable name."""
        claude = _reading(
            Provider.CLAUDE,
            scoped_limits=[ScopedLimit(
                name="Fable", percent=13.0,
                resets_at=_NOW + timedelta(days=3), is_active=False,
            )],
        )
        layout = build_main_layout([claude], _SIZE, now=_NOW)
        assert [b.label for b in layout.tiles[0].bars] == [
            "Session", "Weekly", "Fable",
        ]

    def test_scoped_only_reading_keeps_its_bars(self) -> None:
        """A reading whose only windows are scoped ones is NOT quota-less."""
        zai = _reading(
            Provider.ZAI,
            session_percent=None, session_resets_at=None,
            weekly_percent=None, weekly_resets_at=None,
            scoped_limits=[ScopedLimit(
                name="Monthly", percent=9.0,
                resets_at=_NOW + timedelta(days=22), is_active=False,
            )],
        )
        layout = build_main_layout(
            [zai], _SIZE, now=_NOW,
        )
        assert [b.label for b in layout.tiles[0].bars] == ["Monthly"]
        assert layout.tiles[0].detail is None

    def test_quotaless_tile_carries_detail_and_alert_colour(self) -> None:
        """A genuinely quota-less tile must carry its detail line (rendered by
        the GUI in place of bars) tinted by the volume alert, so it does not
        paint as a bare title."""
        from usage_dashboard.shared.models import ALERT_WARN

        quotaless = _reading(
            Provider.OLLAMA,
            session_percent=None, session_resets_at=None,
            weekly_percent=None, weekly_resets_at=None,
            detail="week req 9 tok 2M", alert=ALERT_WARN,
        )
        layout = build_main_layout([quotaless], _SIZE, now=_NOW)
        tile = layout.tiles[0]
        assert tile.bars == []
        assert tile.detail == "week req 9 tok 2M"
        assert tile.detail_color == fmt.ORANGE

    def test_overhead_gives_equal_row_heights(self) -> None:
        # With tile_overhead specified, a 3-bar tile and a 2-bar tile get
        # equal row heights: (tile_h - overhead) / n_bars is the same.
        # Without the overhead-aware distribution, the 2-bar tile's bars are
        # squished and the 3-bar tile gets excess bottom padding.
        claude = _reading(
            Provider.CLAUDE,
            scoped_limits=[ScopedLimit(
                name="Fable", percent=13.0,
                resets_at=_NOW + timedelta(days=3), is_active=False,
            )],
        )
        readings = [claude, _reading(Provider.CODEX)]
        overhead = 80  # arbitrary; the invariant holds for any value
        layout = build_main_layout(
            readings, _SIZE, now=_NOW, tile_overhead=overhead,
        )
        by = {t.provider: t for t in layout.tiles}
        claude_h = by[Provider.CLAUDE].rect.h
        codex_h = by[Provider.CODEX].rect.h
        assert len(by[Provider.CLAUDE].bars) == 3
        assert len(by[Provider.CODEX].bars) == 2
        # (tile_h - overhead) / n_bars should be equal (within 1px for
        # integer division).
        claude_row = (claude_h - overhead) // 3
        codex_row = (codex_h - overhead) // 2
        assert abs(claude_row - codex_row) <= 1, (
            f"row heights differ: Claude={claude_row}, Codex={codex_row}"
        )

    def test_tiles_within_bounds_and_nonoverlapping(self) -> None:
        layout = build_main_layout(_all_configured(), _SIZE, now=_NOW)
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
        layout = build_main_layout(_all_configured(), _SIZE, now=_NOW)
        claude = layout.tiles[0]
        session, weekly = claude.bars
        assert session.fraction == 0.5
        assert session.color == fmt.GREEN  # 50%
        assert weekly.fraction == 0.9
        assert weekly.color == fmt.RED     # 90%

    def test_full_width_tile_uses_full_labels(self) -> None:
        layout = build_main_layout([_reading(Provider.CLAUDE)], _SIZE, now=_NOW)
        labels = [b.label for b in layout.tiles[0].bars]
        assert labels == ["Session", "Weekly"]
        assert layout.tiles[0].compact is False

    def test_paired_tiles_use_compact_labels(self) -> None:
        layout = build_main_layout(_all_configured(), _SIZE, now=_NOW)
        by = {t.provider: t for t in layout.tiles}
        assert [b.label for b in by[Provider.ZAI].bars] == ["S", "W"]
        assert [b.label for b in by[Provider.OLLAMA].bars] == ["S", "W"]
        assert by[Provider.ZAI].compact is True
        assert by[Provider.OLLAMA].compact is True

    def test_ollama_subtitle_hidden_when_paired(self) -> None:
        reading = _reading(Provider.OLLAMA, models=[
            ModelUsage(name="minimax-m3", requests=100, share_percent=80.0),
        ])
        layout = build_main_layout([_reading(Provider.ZAI), reading], _SIZE, now=_NOW)
        ollama = next(t for t in layout.tiles if t.provider is Provider.OLLAMA)
        assert ollama.subtitle == ""
        assert ollama.compact is True

    def test_ollama_subtitle_shown_when_full_width(self) -> None:
        reading = _reading(Provider.OLLAMA, models=[
            ModelUsage(name="minimax-m3", requests=100, share_percent=80.0),
        ])
        layout = build_main_layout([reading], _SIZE, now=_NOW)
        ollama = layout.tiles[0]
        assert "minimax-m3" in ollama.subtitle
        assert ollama.compact is False

    def test_scoped_limit_renders_extra_claude_bar(self) -> None:
        # A Fable-scoped weekly limit becomes a third bar on the Claude tile,
        # after Session and Weekly, labelled by the model name.
        claude = _reading(
            Provider.CLAUDE,
            scoped_limits=[
                ScopedLimit(
                    name="Fable",
                    percent=13.0,
                    resets_at=_NOW + timedelta(days=3),
                    is_active=False,
                )
            ],
        )
        layout = build_main_layout([claude], _SIZE, now=_NOW)
        bars = layout.tiles[0].bars
        assert [b.label for b in bars] == ["Session", "Weekly", "Fable"]
        assert bars[2].fraction == 0.13
        assert bars[2].color == fmt.GREEN  # 13%

    def test_no_scoped_bars_for_providers_without_limits(self) -> None:
        # zai/ollama readings carry no scoped_limits, so no extra bars appear.
        layout = build_main_layout([_reading(Provider.ZAI)], _SIZE, now=_NOW)
        assert [b.label for b in layout.tiles[0].bars] == ["Session", "Weekly"]

    def test_codex_weekly_only_single_bar(self) -> None:
        # Weekly-only mode: Codex with session_percent=None should render
        # only the Weekly bar, not a grayed "N/A" Session bar.
        codex = _reading(
            Provider.CODEX,
            session_percent=None, session_resets_at=None,
        )
        layout = build_main_layout([codex], _SIZE, now=_NOW)
        bars = layout.tiles[0].bars
        assert len(bars) == 1
        assert bars[0].label == "Weekly"
        assert bars[0].fraction == 0.9  # 90% from _reading default

    def test_detail_weekly_only_omits_session(self) -> None:
        # Detail view for weekly-only Codex should not show a Session line.
        codex = _reading(
            Provider.CODEX,
            session_percent=None, session_resets_at=None,
        )
        detail = build_detail_layout(codex, now=_NOW)
        labels = [line.label for line in detail.lines]
        assert "Session" not in labels
        assert "Weekly" in labels

    def test_zai_title_green_offpeak(self) -> None:
        # Sat 2026-01-10 (weekend): never peak -> title tints green.
        layout = build_main_layout(_all_configured(), _SIZE, now=_NOW)
        by = {t.provider: t for t in layout.tiles}
        assert by[Provider.ZAI].title_color == fmt.GREEN

    def test_zai_title_orange_at_peak_boundary(self) -> None:
        # Mon 2026-01-12 07:00 UTC = Mon 15:00 UTC+8 -> inside peak hours.
        monday_peak = datetime(2026, 1, 12, 7, 0, 0, tzinfo=timezone.utc)
        layout = build_main_layout(_all_configured(), _SIZE, now=monday_peak)
        by = {t.provider: t for t in layout.tiles}
        assert by[Provider.ZAI].title_color == fmt.ORANGE

    def test_zai_subtitle_counts_down_to_peak_start_when_offpeak(self) -> None:
        # Sat 2026-01-10 (weekend, off-peak): next peak begins Mon 14:00 UTC+8
        # (= Mon 06:00 UTC) -> 1d 18h from _NOW.
        layout = build_main_layout(_all_configured(), _SIZE, now=_NOW)
        by = {t.provider: t for t in layout.tiles}
        assert by[Provider.ZAI].subtitle == "peak in 1d 18h"

    def test_zai_subtitle_counts_down_to_peak_end_when_in_peak(self) -> None:
        # Mon 2026-01-12 07:00 UTC = 15:00 UTC+8 (in peak); the window closes
        # at 18:00 UTC+8 (= 10:00 UTC): "ends in 3h 0m".
        monday_peak = datetime(2026, 1, 12, 7, 0, 0, tzinfo=timezone.utc)
        layout = build_main_layout(_all_configured(), _SIZE, now=monday_peak)
        by = {t.provider: t for t in layout.tiles}
        assert by[Provider.ZAI].subtitle == "ends in 3h 0m"

    def test_other_tiles_keep_white_title(self) -> None:
        layout = build_main_layout(_all_configured(), _SIZE, now=_NOW)
        by = {t.provider: t for t in layout.tiles}
        assert by[Provider.CLAUDE].title_color == fmt.TEXT
        assert by[Provider.OLLAMA].title_color == fmt.TEXT

    def test_accent_is_worst_bar_color(self) -> None:
        layout = build_main_layout(_all_configured(), _SIZE, now=_NOW)
        # Claude has a 90% weekly bar -> red accent.
        assert layout.tiles[0].accent == fmt.RED

    def test_status_text_mentions_count(self) -> None:
        layout = build_main_layout(_all_configured(), _SIZE, now=_NOW)
        assert "3 providers" in layout.status_text

    def test_status_count_matches_touch_tiles_not_raw_readings(self) -> None:
        layout = build_main_layout(
            [
                _reading(Provider.CLAUDE),
                _reading(Provider.CLAUDE_WORK),
                _reading(Provider.OPENCODE),
            ],
            _SIZE,
            now=_NOW,
        )

        assert [tile.provider for tile in layout.tiles] == [Provider.CLAUDE]
        assert "1 provider" in layout.status_text

    def test_status_text_shows_refresh_interval(self) -> None:
        layout = build_main_layout(_all_configured(), _SIZE, now=_NOW, refresh_interval=300)
        assert "refresh 5m" in layout.status_text

    def test_status_text_omits_refresh_when_none(self) -> None:
        layout = build_main_layout(_all_configured(), _SIZE, now=_NOW)
        assert "refresh" not in layout.status_text

    def test_status_text_not_utc(self) -> None:
        layout = build_main_layout(_all_configured(), _SIZE, now=_NOW)
        assert "UTC" not in layout.status_text

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
        layout = build_main_layout(_all_configured(), (720, 1280), now=_NOW)
        for t in layout.tiles:
            assert t.rect.x + t.rect.w <= 720
            assert t.rect.y + t.rect.h <= 1280

    def test_narrow_width_stacks_paired_providers(self) -> None:
        """A tiny portrait panel uses a summary matrix, not collapsed rows."""
        layout = build_main_layout(_all_configured(), (240, 320), now=_NOW)
        assert [tile.provider for tile in layout.tiles] == [
            Provider.CLAUDE, Provider.ZAI, Provider.OLLAMA,
        ]
        assert all(tile.ultra_compact for tile in layout.tiles)
        assert len({tile.rect.y for tile in layout.tiles}) == 2
        assert len({tile.rect.x for tile in layout.tiles}) == 2
        for tile in layout.tiles:
            assert tile.rect.x >= 0
            assert tile.rect.x + tile.rect.w <= 240
            assert tile.rect.y + tile.rect.h <= layout.status_rect.y

    def test_four_provider_tiny_grid_is_orientation_aware(self) -> None:
        readings = _all_configured() + [_reading(Provider.CODEX)]

        portrait = build_main_layout(readings, (240, 320), now=_NOW)
        assert all(tile.ultra_compact for tile in portrait.tiles)
        assert len({tile.rect.y for tile in portrait.tiles}) == 2
        assert all(
            sum(tile.rect.y == row_y for tile in portrait.tiles) == 2
            for row_y in {tile.rect.y for tile in portrait.tiles}
        )

        landscape = build_main_layout(readings, (320, 240), now=_NOW)
        assert all(tile.ultra_compact for tile in landscape.tiles)
        assert len({tile.rect.y for tile in landscape.tiles}) == 1
        assert len({tile.rect.x for tile in landscape.tiles}) == 4

        for layout, size in ((portrait, (240, 320)), (landscape, (320, 240))):
            for tile in layout.tiles:
                assert tile.rect.x >= 0 and tile.rect.y >= 0
                assert tile.rect.x + tile.rect.w <= size[0]
                assert tile.rect.y + tile.rect.h <= layout.status_rect.y
                assert tile.rect.w >= MIN_TOUCH_TARGET
                assert tile.rect.h >= MIN_TOUCH_TARGET
            for index, first in enumerate(layout.tiles):
                for second in layout.tiles[index + 1:]:
                    overlap_x = first.rect.x < second.rect.x + second.rect.w
                    overlap_y = first.rect.y < second.rect.y + second.rect.h
                    overlap_x = overlap_x and second.rect.x < first.rect.x + first.rect.w
                    overlap_y = overlap_y and second.rect.y < first.rect.y + first.rect.h
                    assert not (overlap_x and overlap_y)

    def test_normal_four_provider_layout_keeps_established_rows(self) -> None:
        layout = build_main_layout(
            _all_configured() + [_reading(Provider.CODEX)],
            (1280, 720),
            now=_NOW,
        )
        by = {tile.provider: tile for tile in layout.tiles}
        assert not any(tile.ultra_compact for tile in layout.tiles)
        assert by[Provider.CLAUDE].compact is False
        assert by[Provider.CODEX].compact is False
        assert by[Provider.ZAI].compact is True
        assert by[Provider.OLLAMA].compact is True
        assert by[Provider.ZAI].rect.y == by[Provider.OLLAMA].rect.y

    def test_status_overlay_regions_fit_portrait_and_narrow_screens(self) -> None:
        for size in (
            (240, 320), (390, 844), (720, 1280),
            (320, 240), (844, 390), (1280, 720),
        ):
            width, height = size
            overlay = build_status_overlay(size)
            panel = overlay.panel
            assert panel.x >= 0 and panel.y >= 0
            assert panel.x + panel.w <= width
            assert panel.y + panel.h <= height
            for region in (
                overlay.diag_rect,
                overlay.brightness.region,
                overlay.brightness.minus,
                overlay.brightness.plus,
                overlay.brightness.level_rect,
            ):
                assert panel.contains(region.x, region.y)
                assert panel.contains(region.x + region.w - 1, region.y + region.h - 1)
            assert (
                overlay.diag_rect.x + overlay.diag_rect.w
                <= overlay.brightness.region.x
            )
            assert overlay.brightness.minus.w >= MIN_TOUCH_TARGET
            assert overlay.brightness.minus.h >= MIN_TOUCH_TARGET
            assert overlay.brightness.plus.w >= MIN_TOUCH_TARGET
            assert overlay.brightness.plus.h >= MIN_TOUCH_TARGET


class TestHitTest:
    def test_tap_inside_tile_returns_provider(self) -> None:
        layout = build_main_layout(_all_configured(), _SIZE, now=_NOW)
        r = layout.tiles[2].rect
        assert hit_test(layout, (r.x + r.w // 2, r.y + r.h // 2)) is Provider.OLLAMA

    def test_tap_in_status_bar_returns_none(self) -> None:
        layout = build_main_layout(_all_configured(), _SIZE, now=_NOW)
        sr = layout.status_rect
        assert hit_test(layout, (sr.x + 5, sr.y + 5)) is None


class TestRefreshButton:
    """On-demand refresh button (WI-012): a tap target at the right edge of
    the status bar, checked before the status-bar overlay."""

    def test_refresh_button_inside_status_bar(self) -> None:
        layout = build_main_layout(_all_configured(), _SIZE, now=_NOW)
        assert layout.refresh_rect is not None
        sr = layout.status_rect
        r = layout.refresh_rect
        assert sr.contains(r.x, r.y)
        assert sr.contains(r.x + r.w - 1, r.y + r.h - 1)
        assert r.w >= MIN_TOUCH_TARGET
        assert r.h >= MIN_TOUCH_TARGET

    def test_refresh_button_anchored_to_right_edge(self) -> None:
        layout = build_main_layout(_all_configured(), _SIZE, now=_NOW)
        r = layout.refresh_rect
        assert r is not None
        # Button's right edge sits inside the status bar's right edge, leaving
        # the footer note (drawn just left of it) room.
        assert r.x + r.w <= layout.status_rect.x + layout.status_rect.w
        assert r.w == r.h  # a square finger target

    def test_refresh_hit_test_inside(self) -> None:
        layout = build_main_layout(_all_configured(), _SIZE, now=_NOW)
        r = layout.refresh_rect
        assert r is not None
        assert refresh_hit_test(layout, (r.x + r.w // 2, r.y + r.h // 2)) is True

    def test_refresh_hit_test_outside(self) -> None:
        layout = build_main_layout(_all_configured(), _SIZE, now=_NOW)
        # Top-left corner (over tiles) and the left end of the status bar
        # (status text) are not the button.
        assert refresh_hit_test(layout, (5, 5)) is False
        sr = layout.status_rect
        assert refresh_hit_test(layout, (sr.x + 5, sr.y + 5)) is False

    def test_refresh_pending_flag_propagates(self) -> None:
        layout = build_main_layout(
            _all_configured(), _SIZE, now=_NOW, refresh_pending=True,
        )
        assert layout.refresh_pending is True
        idle = build_main_layout(_all_configured(), _SIZE, now=_NOW)
        assert idle.refresh_pending is False


class TestViewTransitions:
    def test_tap_tile_opens_detail(self) -> None:
        layout = build_main_layout(_all_configured(), _SIZE, now=_NOW)
        r = layout.tiles[0].rect
        state = tap_transition(ViewState(), layout, (r.x + 5, r.y + 5))
        assert state.detail_provider is Provider.CLAUDE

    def test_tap_outside_tile_stays_on_grid(self) -> None:
        layout = build_main_layout(_all_configured(), _SIZE, now=_NOW)
        sr = layout.status_rect
        state = tap_transition(ViewState(), layout, (sr.x + 1, sr.y + 1))
        assert state.detail_provider is None

    def test_tap_in_detail_returns_to_grid(self) -> None:
        layout = build_main_layout(_all_configured(), _SIZE, now=_NOW)
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
        # A quota-less reading (no percentages) renders its detail text line.
        zai = _reading(
            Provider.ZAI, session_percent=None, weekly_percent=None,
            session_resets_at=None, weekly_resets_at=None, detail="req 9 tok 2M",
        )
        detail = build_detail_layout(zai, now=_NOW)
        values = [line.value for line in detail.lines]
        assert "req 9 tok 2M" in values


class TestModelBreakdown:
    _MODELS = [
        ModelUsage(name="minimax-m3", requests=1841, share_percent=68.0),
        ModelUsage(name="nemotron-3-ultra", requests=588, share_percent=27.5),
        ModelUsage(name="glm-5.2", requests=49, share_percent=2.4),
    ]

    def test_ollama_title_includes_top_models(self) -> None:
        reading = _reading(Provider.OLLAMA, models=self._MODELS)
        layout = build_main_layout([reading], _SIZE, now=_NOW)
        subtitle = layout.tiles[0].subtitle
        assert "minimax-m3 68%" in subtitle
        assert "nemotron-3-ultra 28%" in subtitle
        assert "glm-5.2" not in subtitle

    def test_ollama_title_plain_without_models(self) -> None:
        reading = _reading(Provider.OLLAMA)
        layout = build_main_layout([reading], _SIZE, now=_NOW)
        assert layout.tiles[0].title == "OLLAMA"
        assert layout.tiles[0].subtitle == ""

    def test_zai_title_has_no_model_subtitle(self) -> None:
        reading = _reading(Provider.ZAI, models=[
            ModelUsage(name="search-prime", requests=64, share_percent=64.0),
        ])
        layout = build_main_layout([reading], _SIZE, now=_NOW)
        assert layout.tiles[0].title == "ZAI"

    def test_detail_shows_ollama_model_lines(self) -> None:
        reading = _reading(Provider.OLLAMA, models=self._MODELS)
        detail = build_detail_layout(reading, now=_NOW)
        labels = [line.label for line in detail.lines]
        assert "Models" in labels
        assert "  minimax-m3" in labels
        assert "  nemotron-3-ultra" in labels
        # All three models appear in the detail (not just top 2).
        assert "  glm-5.2" in labels

    def test_detail_shows_zai_tool_lines(self) -> None:
        reading = _reading(Provider.ZAI, models=[
            ModelUsage(name="search-prime", requests=64, share_percent=64.0),
            ModelUsage(name="web-reader", requests=31, share_percent=31.0),
        ])
        detail = build_detail_layout(reading, now=_NOW)
        labels = [line.label for line in detail.lines]
        assert "API tools" in labels
        assert "  search-prime" in labels

    def test_detail_no_model_lines_when_none(self) -> None:
        reading = _reading(Provider.OLLAMA)
        detail = build_detail_layout(reading, now=_NOW)
        labels = [line.label for line in detail.lines]
        assert "Models" not in labels


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
            Provider.CLAUDE, Provider.ZAI, Provider.OLLAMA
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


class TestClaudeWorkAccount:
    def _claude(self, **over: object) -> Reading:
        return _reading(Provider.CLAUDE, **over)

    def _work(self, **over: object) -> Reading:
        return _reading(Provider.CLAUDE_WORK, **over)

    def test_single_account_is_unchanged(self) -> None:
        # No work account: two untagged, unmuted bars (identical to before).
        layout = build_main_layout([self._claude()], _SIZE, now=_NOW)
        assert [t.provider for t in layout.tiles] == [Provider.CLAUDE]
        bars = layout.tiles[0].bars
        assert len(bars) == 2
        assert all(b.account == "" and not b.muted for b in bars)

    def test_work_account_folds_into_claude_tile(self) -> None:
        layout = build_main_layout(
            [self._claude(), self._work()] , _SIZE, now=_NOW
        )
        # No separate CLAUDE_WORK tile.
        assert [t.provider for t in layout.tiles] == [Provider.CLAUDE]
        bars = layout.tiles[0].bars
        assert len(bars) == 4
        assert [b.account for b in bars] == ["me", "me", "work", "work"]
        assert [b.muted for b in bars] == [False, False, True, True]

    def test_work_only_still_shows_a_claude_tile(self) -> None:
        layout = build_main_layout([self._work()], _SIZE, now=_NOW)
        assert [t.provider for t in layout.tiles] == [Provider.CLAUDE]
        assert len(layout.tiles[0].bars) == 2

    def test_work_account_does_not_add_a_fifth_tile(self) -> None:
        readings = [
            self._claude(), self._work(),
            _reading(Provider.ZAI), _reading(Provider.OLLAMA),
        ]
        layout = build_main_layout(readings, _SIZE, now=_NOW)
        assert [t.provider for t in layout.tiles] == [
            Provider.CLAUDE, Provider.ZAI, Provider.OLLAMA,
        ]

    def test_detail_secondary_appends_work_lines(self) -> None:
        detail = build_detail_layout(
            self._claude(), now=_NOW, secondary=("work", self._work())
        )
        labels = [line.label for line in detail.lines]
        assert "— work —" in labels
        # Session appears twice: once per account.
        assert labels.count("Session") == 2


def test_detail_line_is_coloured_by_the_volume_alert():
    # `alert` had no renderer on the Pi after umans was retired, so z.ai's
    # warn/crit tiers were computed and discarded. The detail view carries the
    # number, so it carries the cue.
    from usage_dashboard.client import format as fmt

    def detail_colour(alert):
        r = Reading(
            provider=Provider.ZAI, status=ReadingStatus.CURRENT,
            session_percent=5.0, weekly_percent=93.0,
            session_resets_at=None, weekly_resets_at=None,
            fetched_at=_NOW, stale=False,
            detail="week req 2273  tok 283.8M", alert=alert,
        )
        line = next(
            ln for ln in build_detail_layout(r, _NOW).lines if ln.label == "Detail"
        )
        return line.color

    assert detail_colour("none") == fmt.TEXT
    assert detail_colour("warn") == fmt.ORANGE
    assert detail_colour("crit") == fmt.RED


def test_zai_subtitle_matches_the_title_colour():
    # The z.ai subtitle is the peak-window countdown — the same signal as the
    # title tint — so it tracks the title rather than reading as neutral grey.
    from usage_dashboard.client import format as fmt

    def zai_tile(now):
        layout = build_main_layout(_all_configured(), (1280, 720), now=now)
        return next(t for t in layout.tiles if t.provider is Provider.ZAI)

    # 2026-08-03 is a Monday. Peak = Mon-Fri 14:00-18:00 UTC+8 == 06:00-10:00 UTC.
    in_peak = zai_tile(datetime(2026, 8, 3, 7, 0))
    off_peak = zai_tile(datetime(2026, 8, 3, 12, 0))

    assert in_peak.title_color == fmt.ORANGE
    assert in_peak.subtitle_color == in_peak.title_color
    assert off_peak.title_color == fmt.GREEN
    assert off_peak.subtitle_color == off_peak.title_color


def test_non_zai_subtitle_stays_neutral():
    # Ollama's subtitle is a model breakdown, not a peak signal.
    from usage_dashboard.client import format as fmt

    layout = build_main_layout(_all_configured(), (1280, 720), now=_NOW)
    for tile in layout.tiles:
        if tile.provider is not Provider.ZAI:
            assert tile.subtitle_color == fmt.GRAY
