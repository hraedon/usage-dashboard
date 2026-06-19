"""Resolution-independent layout model for the touch GUI.

This is the *what to draw*, computed purely from readings and the screen size —
no pygame, no I/O — so it can be unit-tested. ``gui.py`` is the thin layer that
blits these descriptors and routes touches through :func:`hit_test`.

Geometry is derived from the screen dimensions (margins/fonts as fractions), so
the same code lays out an 800x480 panel, a portrait 720x1280, or a dev window.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from usage_dashboard.client import format as fmt
from usage_dashboard.shared.models import Provider, Reading

# Fixed slot order so a provider always lands in the same place frame to frame.
_PROVIDER_ORDER: list[Provider] = [
    Provider.CLAUDE,
    Provider.ZAI,
    Provider.OLLAMA,
    Provider.UMANS,
]

Color = tuple[int, int, int]


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    def contains(self, px: int, py: int) -> bool:
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h


@dataclass(frozen=True)
class BarSpec:
    label: str
    percent_text: str
    fraction: float  # 0.0–1.0 of the track to fill
    color: Color
    reset_text: str
    reset_highlight: bool


@dataclass(frozen=True)
class TileSpec:
    provider: Provider
    title: str           # includes any [stale]/[offline] suffix
    rect: Rect
    bars: list[BarSpec]
    detail: str | None   # quota-less providers (umans) show this instead of bars
    accent: Color        # worst-of bar colours, for a status edge


@dataclass(frozen=True)
class MainLayout:
    size: tuple[int, int]
    tiles: list[TileSpec]
    status_text: str
    status_rect: Rect


@dataclass(frozen=True)
class DetailLine:
    label: str
    value: str
    color: Color


@dataclass(frozen=True)
class DetailLayout:
    provider: Provider
    title: str
    lines: list[DetailLine] = field(default_factory=list)


def _grid_dimensions(n: int) -> tuple[int, int]:
    """Columns, rows for *n* tiles: one column for 1, else two columns."""
    if n <= 1:
        return 1, 1
    cols = 2
    rows = (n + cols - 1) // cols
    return cols, rows


def _bars_for(reading: Reading, now: datetime | None) -> list[BarSpec]:
    bars: list[BarSpec] = []
    for label, pct, reset in (
        ("Session", reading.session_percent, reading.session_resets_at),
        ("Weekly", reading.weekly_percent, reading.weekly_resets_at),
    ):
        reset_text, highlight = fmt.format_countdown(reset, now=now)
        bars.append(
            BarSpec(
                label=label,
                percent_text=fmt.percent_text(pct),
                fraction=(min(pct, 100.0) / 100.0) if pct is not None else 0.0,
                color=fmt.bar_color(pct),
                reset_text=reset_text,
                reset_highlight=highlight,
            )
        )
    return bars


def _accent(bars: list[BarSpec], detail: str | None) -> Color:
    """The 'worst' bar colour (red > orange > green > gray) for a status edge."""
    if not bars:
        return fmt.GRAY if detail is None else fmt.GREEN
    rank = {fmt.RED: 3, fmt.ORANGE: 2, fmt.GREEN: 1, fmt.GRAY: 0}
    return max((b.color for b in bars), key=lambda c: rank.get(c, 0))


def build_main_layout(
    readings: list[Reading],
    size: tuple[int, int],
    now: datetime | None = None,
) -> MainLayout:
    """Lay out provider tiles in a grid plus a bottom status bar."""
    width, height = size
    by_provider = {r.provider: r for r in readings}
    ordered = [by_provider[p] for p in _PROVIDER_ORDER if p in by_provider]

    margin = max(4, round(width * 0.02))
    status_h = max(18, round(height * 0.08))
    grid_h = height - status_h - margin

    cols, rows = _grid_dimensions(len(ordered)) if ordered else (1, 1)
    cell_w = (width - margin * (cols + 1)) // cols
    cell_h = (grid_h - margin * (rows + 1)) // max(rows, 1)

    tiles: list[TileSpec] = []
    for idx, reading in enumerate(ordered):
        col = idx % cols
        row = idx // cols
        rect = Rect(
            x=margin + col * (cell_w + margin),
            y=margin + row * (cell_h + margin),
            w=cell_w,
            h=cell_h,
        )
        # umans has no percentages — show its detail string instead of bars.
        is_quotaless = (
            reading.session_percent is None and reading.weekly_percent is None
        )
        bars = [] if is_quotaless else _bars_for(reading, now)
        detail = reading.detail if is_quotaless else None
        tiles.append(
            TileSpec(
                provider=reading.provider,
                title=reading.provider.value.upper() + fmt.status_suffix(reading),
                rect=rect,
                bars=bars,
                detail=detail,
                accent=_accent(bars, detail),
            )
        )

    status_rect = Rect(x=0, y=height - status_h, w=width, h=status_h)
    return MainLayout(
        size=size,
        tiles=tiles,
        status_text=_status_text(readings, now),
        status_rect=status_rect,
    )


def _status_text(readings: list[Reading], now: datetime | None) -> str:
    if not readings:
        return "Waiting for data…"
    latest = max(r.fetched_at for r in readings)
    return f"Updated {latest.strftime('%H:%M:%S')} UTC · {len(readings)} providers"


def rotate_touch_norm(nx: float, ny: float, degrees: int) -> tuple[float, float]:
    """Map a device-normalised touch point onto the *rotated* framebuffer.

    Under the KMS console / SDL ``kmsdrm`` path there is no compositor to apply
    a libinput transform matrix, so the touch controller keeps reporting in the
    panel's native (portrait) frame even after ``video=DSI-1:...,rotate=N`` has
    rotated what's drawn. This maps the normalised device point ``(nx, ny)`` to
    the normalised on-screen point so a tap lands on the tile under the finger.

    *degrees* is the clockwise display rotation (0/90/180/270); it should match
    the ``rotate=`` value in ``cmdline.txt``. If taps come out mirrored, swap
    90 ↔ 270 (panel handedness varies) — all four cases are reachable here.
    """
    d = degrees % 360
    if d == 90:
        return ny, 1.0 - nx
    if d == 180:
        return 1.0 - nx, 1.0 - ny
    if d == 270:
        return 1.0 - ny, nx
    return nx, ny


def hit_test(layout: MainLayout, pos: tuple[int, int]) -> Provider | None:
    """Provider whose tile contains *pos*, or None."""
    px, py = pos
    for tile in layout.tiles:
        if tile.rect.contains(px, py):
            return tile.provider
    return None


@dataclass(frozen=True)
class ViewState:
    """Which screen is showing. ``detail_provider`` None means the main grid."""

    detail_provider: Provider | None = None


def tap_transition(
    state: ViewState, layout: MainLayout, pos: tuple[int, int]
) -> ViewState:
    """Next view after a tap at *pos*.

    From the detail screen, any tap returns to the grid. From the grid, a tap
    inside a tile opens that provider's detail; a tap elsewhere does nothing.
    """
    if state.detail_provider is not None:
        return ViewState(detail_provider=None)
    provider = hit_test(layout, pos)
    if provider is None:
        return state
    return ViewState(detail_provider=provider)


def build_detail_layout(reading: Reading, now: datetime | None = None) -> DetailLayout:
    """Full-screen breakdown for a single provider (the tap-through view)."""
    lines: list[DetailLine] = []
    if reading.session_percent is not None or reading.weekly_percent is not None:
        s_reset, _ = fmt.format_countdown(reading.session_resets_at, now=now)
        w_reset, _ = fmt.format_countdown(reading.weekly_resets_at, now=now)
        lines.append(
            DetailLine("Session", fmt.percent_text(reading.session_percent),
                       fmt.bar_color(reading.session_percent))
        )
        if s_reset:
            lines.append(DetailLine("  resets in", s_reset, fmt.GRAY))
        lines.append(
            DetailLine("Weekly", fmt.percent_text(reading.weekly_percent),
                       fmt.bar_color(reading.weekly_percent))
        )
        if w_reset:
            lines.append(DetailLine("  resets in", w_reset, fmt.GRAY))
    if reading.detail:
        lines.append(DetailLine("Detail", reading.detail, fmt.TEXT))
    lines.append(DetailLine("Status", reading.status.value, fmt.TEXT))
    lines.append(
        DetailLine("Fetched", reading.fetched_at.strftime("%Y-%m-%d %H:%M:%S"), fmt.GRAY)
    )
    return DetailLayout(
        provider=reading.provider,
        title=reading.provider.value.upper() + fmt.status_suffix(reading),
        lines=lines,
    )
