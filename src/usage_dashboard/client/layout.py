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
from usage_dashboard.shared.models import (
    THROTTLE_BOXED,
    THROTTLE_LOW,
    ModelUsage,
    Provider,
    Reading,
)

# Fixed tile order so a provider always lands in the same place frame to frame.
# CLAUDE_WORK is intentionally absent: it has no tile of its own — it folds into
# the CLAUDE tile as a second, muted set of bars.
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
    account: str = ""   # non-empty (e.g. "work") tags a second account's bars
    muted: bool = False  # render the fill quieter (the secondary account)


@dataclass(frozen=True)
class TileSpec:
    provider: Provider
    title: str           # includes any [stale]/[offline] suffix
    rect: Rect
    bars: list[BarSpec]
    detail: str | None   # quota-less providers (umans) show this instead of bars
    accent: Color        # worst-of bar colours, for a status edge
    subtitle: str = ""   # right-aligned model breakdown for the tile header


@dataclass(frozen=True)
class MainLayout:
    size: tuple[int, int]
    tiles: list[TileSpec]
    status_text: str
    status_rect: Rect
    footer_note: str = ""  # quota-less provider (umans) summary, shown by the status bar
    footer_color: Color = fmt.TEXT  # yellow=low priority, red=penalty box


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


def _bars_for(
    reading: Reading,
    now: datetime | None,
    account: str = "",
    muted: bool = False,
) -> list[BarSpec]:
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
                # Keep the semantic palette colour (so the accent ranking still
                # works); the GUI mutes the fill when ``muted`` is set.
                color=fmt.bar_color(pct),
                reset_text=reset_text,
                reset_highlight=highlight,
                account=account,
                muted=muted,
            )
        )
    return bars


def _claude_tile_bars(
    by_provider: dict[Provider, Reading], now: datetime | None
) -> list[BarSpec]:
    """Bars for the Claude tile, merging the personal and (optional) work
    accounts. With only one account the bars are untagged and unmuted — i.e.
    identical to a single-provider tile."""
    accounts = [
        (Provider.CLAUDE, "me"),
        (Provider.CLAUDE_WORK, "work"),
    ]
    present = [(p, label) for p, label in accounts if p in by_provider]
    if len(present) <= 1:
        # Single account: no tag, no muting (unchanged from the original look).
        provider = present[0][0] if present else Provider.CLAUDE
        return _bars_for(by_provider[provider], now)
    bars: list[BarSpec] = []
    for provider, label in present:
        bars.extend(
            _bars_for(by_provider[provider], now, account=label,
                      muted=(provider is Provider.CLAUDE_WORK))
        )
    return bars


def _accent(bars: list[BarSpec], detail: str | None) -> Color:
    """The 'worst' bar colour (red > orange > green > gray) for a status edge."""
    if not bars:
        return fmt.GRAY if detail is None else fmt.GREEN
    rank = {fmt.RED: 3, fmt.ORANGE: 2, fmt.GREEN: 1, fmt.GRAY: 0}
    return max((b.color for b in bars), key=lambda c: rank.get(c, 0))


def _model_subtitle(models: list[ModelUsage] | None, top_n: int = 2) -> str:
    """Compact 'top N models' string for the tile title.

    e.g. ``minimax-m3 68% · nemotron-3-ultra 28%``
    """
    if not models:
        return ""
    top = [m for m in models if m.share_percent > 0][:top_n]
    return " · ".join(f"{m.name} {m.share_percent:.0f}%" for m in top)


def build_main_layout(
    readings: list[Reading],
    size: tuple[int, int],
    now: datetime | None = None,
    refresh_interval: int | None = None,
) -> MainLayout:
    """Lay out provider tiles in a grid plus a bottom status bar."""
    width, height = size
    by_provider = {r.provider: r for r in readings}

    # Which tiles to show, with the reading that drives each tile's title. The
    # Claude tile appears if either Claude account reported, and its title comes
    # from the personal account when present (else the work account).
    tile_plan: list[tuple[Provider, Reading]] = []
    for provider in _PROVIDER_ORDER:
        if provider is Provider.CLAUDE:
            primary = by_provider.get(Provider.CLAUDE) or by_provider.get(Provider.CLAUDE_WORK)
            if primary is not None:
                tile_plan.append((Provider.CLAUDE, primary))
        elif provider is Provider.UMANS:
            # umans is quota-less; it goes in the footer, not a tile (see below).
            continue
        elif provider in by_provider:
            tile_plan.append((provider, by_provider[provider]))

    # umans summary for the status-bar footer (quota-less: just its detail).
    umans = by_provider.get(Provider.UMANS)
    footer_note = ""
    footer_color = fmt.TEXT
    if umans is not None:
        # umans has no quota to colour by; throttle severity is its signal.
        # boxed (account locked) = red, low priority = yellow, else default.
        if umans.throttle == THROTTLE_BOXED:
            # Replace the (now-moot) req/tok metrics with a countdown to when
            # the penalty box clears.
            text, _ = fmt.format_countdown(umans.session_resets_at, now)
            footer_note = f"UMANS boxed {text}".strip()
            footer_color = fmt.RED
        elif umans.throttle == THROTTLE_LOW:
            footer_note = f"UMANS {umans.detail}".strip() if umans.detail else "UMANS"
            footer_color = fmt.YELLOW
        else:
            footer_note = f"UMANS {umans.detail}".strip() if umans.detail else "UMANS"

    margin = max(4, round(width * 0.02))
    status_h = max(18, round(height * 0.10))
    grid_h = height - status_h - margin

    # Single-column vertical stack.
    cols, rows = (1, max(len(tile_plan), 1))
    cell_w = (width - margin * (cols + 1)) // cols
    cell_h = (grid_h - margin * (rows + 1)) // max(rows, 1)

    tiles: list[TileSpec] = []
    for idx, (provider, reading) in enumerate(tile_plan):
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
        if provider is Provider.CLAUDE:
            bars = _claude_tile_bars(by_provider, now)
            detail = None
        else:
            bars = [] if is_quotaless else _bars_for(reading, now)
            detail = reading.detail if is_quotaless else None
        # Build the title: provider name + status. Model breakdown goes in
        # subtitle (right-aligned by the GUI), not in the title string.
        title = provider.value.upper() + fmt.status_suffix(reading)
        subtitle = ""
        if provider is Provider.OLLAMA:
            subtitle = _model_subtitle(reading.models)
        tiles.append(
            TileSpec(
                provider=provider,
                title=title,
                rect=rect,
                bars=bars,
                detail=detail,
                accent=_accent(bars, detail),
                subtitle=subtitle,
            )
        )

    status_rect = Rect(x=0, y=height - status_h, w=width, h=status_h)
    return MainLayout(
        size=size,
        tiles=tiles,
        status_text=_status_text(readings, now, refresh_interval),
        status_rect=status_rect,
        footer_note=footer_note,
        footer_color=footer_color,
    )


def _status_text(
    readings: list[Reading],
    now: datetime | None,
    refresh_interval: int | None = None,
) -> str:
    if not readings:
        return "Waiting for data…"
    latest = max(r.fetched_at for r in readings)
    local = fmt.to_local(latest)
    text = f"Updated {local.strftime('%H:%M:%S')}"
    if refresh_interval is not None:
        text += f" · refresh {fmt.format_interval(refresh_interval)}"
    text += f" · {len(readings)} providers"
    return text


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


def _detail_lines(reading: Reading, now: datetime | None) -> list[DetailLine]:
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
    if reading.models:
        label = "API tools" if reading.provider is Provider.ZAI else "Models"
        lines.append(DetailLine(label, "", fmt.GRAY))
        for m in reading.models:
            lines.append(
                DetailLine(
                    f"  {m.name}",
                    f"{m.share_percent:.0f}% · {m.requests} req",
                    fmt.GRAY,
                )
            )
    lines.append(DetailLine("Status", reading.status.value, fmt.TEXT))
    lines.append(
        DetailLine(
            "Fetched",
            fmt.to_local(reading.fetched_at).strftime("%Y-%m-%d %H:%M:%S"),
            fmt.GRAY,
        )
    )
    return lines


def build_detail_layout(
    reading: Reading,
    now: datetime | None = None,
    secondary: tuple[str, Reading] | None = None,
) -> DetailLayout:
    """Full-screen breakdown for a provider (the tap-through view).

    *secondary* is an optional ``(label, reading)`` for a second account (the
    work Claude login); its lines are appended under a header so one tap shows
    both accounts.
    """
    lines = _detail_lines(reading, now)
    if secondary is not None:
        label, sec_reading = secondary
        lines.append(DetailLine(f"— {label} —", "", fmt.GRAY))
        lines.extend(_detail_lines(sec_reading, now))
    return DetailLayout(
        provider=reading.provider,
        title=reading.provider.value.upper() + fmt.status_suffix(reading),
        lines=lines,
    )
