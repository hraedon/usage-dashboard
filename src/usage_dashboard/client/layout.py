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
    ALERT_CRIT,
    ALERT_WARN,
    THROTTLE_BOXED,
    THROTTLE_LOW,
    THROTTLE_LOW_INTERACTIVITY,
    THROTTLE_RATE_LIMITED,
    ModelUsage,
    Provider,
    Reading,
)

# Fixed tile order so a provider always lands in the same place frame to frame.
# CLAUDE_WORK is intentionally absent: it has no tile of its own — it folds into
# the CLAUDE tile as a second, muted set of bars.
_PROVIDER_ORDER: list[Provider] = [
    Provider.CLAUDE,
    Provider.CODEX,
    Provider.ZAI,
    Provider.OLLAMA,
    Provider.UMANS,
]

# Providers rendered half-width so two consecutive ones share a row, freeing
# vertical space. Claude/Codex stay full-width (Claude carries an extra Fable
# bar and can fold in a work account); z.ai + ollama pair up beneath them.
_PAIRED_PROVIDERS: frozenset[Provider] = frozenset({Provider.ZAI, Provider.OLLAMA})

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
    compact: bool = False  # half-width paired tile: S/W labels, bare countdown


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
    compact: bool = False,
) -> list[BarSpec]:
    bars: list[BarSpec] = []
    # Full-width tiles spell out "Session"/"Weekly"; the compact S/W
    # abbreviations are reserved for the narrow half-width paired tiles,
    # where the freed label width directly extends the bar.
    s_label, w_label = ("S", "W") if compact else ("Session", "Weekly")
    windows: list[tuple[str, float | None, datetime | None]] = [
        (s_label, reading.session_percent, reading.session_resets_at),
        (w_label, reading.weekly_percent, reading.weekly_resets_at),
    ]
    # Per-model scoped windows (e.g. Fable) render as extra bars after the
    # aggregate two; the label is the model name so the tile stays legible.
    for sl in reading.scoped_limits or []:
        windows.append((sl.name, sl.percent, sl.resets_at))
    # Skip windows with no data (e.g. Codex weekly-only mode has no session
    # limit; showing a grayed "N/A" bar is misleading).
    windows = [(lbl, pct, rst) for lbl, pct, rst in windows if pct is not None]
    for label, pct, reset in windows:
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
    by_provider: dict[Provider, Reading], now: datetime | None,
    compact: bool = False,
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
        return _bars_for(by_provider[provider], now, compact=compact)
    bars: list[BarSpec] = []
    for provider, label in present:
        bars.extend(
            _bars_for(by_provider[provider], now, account=label,
                      muted=(provider is Provider.CLAUDE_WORK), compact=compact)
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


def _estimate_tile_overhead(size: tuple[int, int]) -> int:
    """Estimate the fixed per-tile vertical overhead (title + padding) the GUI
    subtracts before drawing bars.  Mirrors the font/pad sizing in
    :class:`DashboardGui` so tile heights give every bar the same row height.
    The GUI passes the *exact* overhead (from the rendered font height); this
    fallback is used by layout-only tests and when no GUI is involved."""
    w, h = size
    pad = max(8, min(w, h) // 40)
    unit = max(18, h // 15)
    title_h = unit * 5 // 4
    # top: pad + title + pad//2;  bottom: pad
    return pad + title_h + pad // 2 + pad


def build_main_layout(
    readings: list[Reading],
    size: tuple[int, int],
    now: datetime | None = None,
    refresh_interval: int | None = None,
    tile_overhead: int | None = None,
) -> MainLayout:
    """Lay out provider tiles in a grid plus a bottom status bar.

    *tile_overhead* is the fixed vertical cost per tile (title + padding) that
    the GUI subtracts before drawing bars.  When provided (by the GUI, which
    knows the real font height), row heights are distributed so that every
    tile's bars get the same row height regardless of bar count.  When None,
    an estimate from the screen size is used.
    """
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
        # boxed (account locked) = red, rate_limited (deprioritized window,
        # still serving) = orange, low-interactivity (heavy-day queueing) =
        # blue matching umans' own banner, low priority = yellow, else the
        # token-volume alert (crit red / warn orange) or default.
        if umans.throttle == THROTTLE_BOXED:
            # Replace the (now-moot) req/tok metrics with a countdown to when
            # the penalty box clears.
            text, _ = fmt.format_countdown(umans.session_resets_at, now)
            footer_note = f"UMANS boxed {text}".strip()
            footer_color = fmt.RED
        elif umans.throttle == THROTTLE_RATE_LIMITED:
            # Deprioritized window: the account is still serving; the window
            # countdown is the actionable signal, so it takes the metrics' spot.
            text, _ = fmt.format_countdown(umans.session_resets_at, now)
            footer_note = f"UMANS rate-limited {text}".strip()
            footer_color = fmt.ORANGE
        elif umans.throttle == THROTTLE_LOW_INTERACTIVITY:
            # Heavy-day queueing: interactive-again is the actionable signal,
            # so the countdown takes the metrics' spot.
            text, _ = fmt.format_countdown(umans.session_resets_at, now)
            footer_note = f"UMANS low-interactivity {text}".strip()
            footer_color = fmt.BLUE
        elif umans.throttle == THROTTLE_LOW:
            footer_note = f"UMANS {umans.detail}".strip() if umans.detail else "UMANS"
            footer_color = fmt.YELLOW
        else:
            footer_note = f"UMANS {umans.detail}".strip() if umans.detail else "UMANS"
            # Unthrottled: the trailing-window token volume is the early
            # warning for the (opaque) heavy-usage penalty.
            if umans.alert == ALERT_CRIT:
                footer_color = fmt.RED
            elif umans.alert == ALERT_WARN:
                footer_color = fmt.ORANGE

    margin = max(4, round(width * 0.02))
    # A slim status band leaves more height for the tiles (the Claude tile in
    # particular needs room for its third bar).
    status_h = max(16, round(height * 0.085))
    grid_h = height - status_h - margin

    # Pack tiles into rows: two consecutive PAIRED providers share a row
    # (half-width each); everything else spans the full width on its own row.
    rows_plan: list[list[tuple[Provider, Reading]]] = []
    idx = 0
    while idx < len(tile_plan):
        provider = tile_plan[idx][0]
        nxt = tile_plan[idx + 1][0] if idx + 1 < len(tile_plan) else None
        if provider in _PAIRED_PROVIDERS and nxt in _PAIRED_PROVIDERS:
            rows_plan.append([tile_plan[idx], tile_plan[idx + 1]])
            idx += 2
        else:
            rows_plan.append([tile_plan[idx]])
            idx += 1

    n_rows = max(len(rows_plan), 1)

    # Build each tile's content first so rows can be sized by bar count: a tile
    # with more bars (Claude, with its extra Fable window) gets a proportionally
    # taller row, so every bar renders at the same height regardless of how many
    # bars its tile has.
    BuiltTile = tuple[Provider, "Reading", list[BarSpec], "str | None", str, str, bool]
    built_rows: list[list[BuiltTile]] = []
    row_weights: list[int] = []
    for row_tiles in rows_plan:
        built: list[BuiltTile] = []
        weight = 1
        is_paired = len(row_tiles) > 1
        for provider, reading in row_tiles:
            # umans has no percentages — show its detail string instead of bars.
            is_quotaless = (
                reading.session_percent is None and reading.weekly_percent is None
            )
            if provider is Provider.CLAUDE:
                bars = _claude_tile_bars(by_provider, now, compact=is_paired)
                detail = None
            else:
                bars = [] if is_quotaless else _bars_for(
                    reading, now, compact=is_paired
                )
                detail = reading.detail if is_quotaless else None
            # Provider name + status; model breakdown goes in the subtitle,
            # but only on full-width tiles — the narrow paired tile is too
            # tight for a model breakdown next to the title.
            title = provider.value.upper() + fmt.status_suffix(reading)
            if provider is Provider.OLLAMA and not is_paired:
                subtitle = _model_subtitle(reading.models)
            else:
                subtitle = ""
            built.append((provider, reading, bars, detail, title, subtitle, is_paired))
            weight = max(weight, len(bars))
        built_rows.append(built)
        row_weights.append(weight)

    total_weight = sum(row_weights)

    if tile_overhead is None:
        tile_overhead = _estimate_tile_overhead((width, height))

    avail = grid_h - margin * (n_rows + 1)
    # Subtract the fixed per-tile overhead before proportional distribution so
    # every tile's bars get the same row height.  Without this the overhead
    # eats a disproportionate share of the shorter tiles (e.g. 2-bar Codex),
    # squishing their bars to the minimum height while the 3-bar Claude tile
    # gets excess bottom padding.
    usable = max(0, avail - tile_overhead * n_rows)
    row_heights: list[int] = []
    used = 0
    for idx, weight in enumerate(row_weights):
        if idx == n_rows - 1:
            row_heights.append(usable - used + tile_overhead)  # last absorbs remainder
        else:
            h = usable * weight // total_weight
            used += h
            row_heights.append(h + tile_overhead)

    tiles: list[TileSpec] = []
    y = margin
    for row_idx, built in enumerate(built_rows):
        cell_h = row_heights[row_idx]
        ncols = len(built)
        cell_w = (width - margin * (ncols + 1)) // ncols
        for col_idx, built_tile in enumerate(built):
            provider, reading, bars, detail, title, subtitle, compact = built_tile
            rect = Rect(
                x=margin + col_idx * (cell_w + margin),
                y=y,
                w=cell_w,
                h=cell_h,
            )
            tiles.append(
                TileSpec(
                    provider=provider,
                    title=title,
                    rect=rect,
                    bars=bars,
                    detail=detail,
                    accent=_accent(bars, detail),
                    subtitle=subtitle,
                    compact=compact,
                )
            )
        y += cell_h + margin

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
    """Which screen is showing. With ``overlay`` False and ``detail_provider``
    None the main grid is up; ``detail_provider`` set shows that provider's
    detail; ``overlay`` True shows the status overlay — unit diagnostics on the
    left, brightness ``−``/``+`` on the right — on top of the grid."""

    detail_provider: Provider | None = None
    overlay: bool = False


@dataclass(frozen=True)
class BrightnessOverlay:
    """Tap regions for the brightness controls: a big ``−`` and ``+`` flanking a
    centre readout, laid into *region* (the overlay's right column)."""

    region: Rect
    minus: Rect
    plus: Rect
    level_rect: Rect


@dataclass(frozen=True)
class StatusOverlay:
    """The status card opened by tapping the "Updated…" line: a *panel* split
    into a left *diag_rect* (unit diagnostics text) and right *brightness*
    controls. A tap outside *panel* closes it."""

    panel: Rect
    diag_rect: Rect
    brightness: BrightnessOverlay


def build_brightness_overlay(region: Rect) -> BrightnessOverlay:
    """Lay the ``−`` | readout | ``+`` controls into *region* as three columns,
    so the same code sizes finger targets on the 5" panel and the dev window."""
    pad = max(8, min(region.w, region.h) // 12)
    title_h = region.h // 5
    row_y = region.y + title_h
    row_h = region.h - title_h - pad
    col_w = (region.w - 2 * pad) // 3
    minus = Rect(region.x + pad, row_y, col_w, row_h)
    plus = Rect(region.x + region.w - pad - col_w, row_y, col_w, row_h)
    level_rect = Rect(
        minus.x + col_w, row_y, region.w - 2 * pad - 2 * col_w, row_h
    )
    return BrightnessOverlay(
        region=region, minus=minus, plus=plus, level_rect=level_rect
    )


def build_status_overlay(size: tuple[int, int]) -> StatusOverlay:
    """Centred status card sized as a fraction of the screen, split into a wider
    left column for diagnostics text and a right column for brightness."""
    width, height = size
    pw, ph = int(width * 0.82), int(height * 0.6)
    px, py = (width - pw) // 2, (height - ph) // 2
    panel = Rect(px, py, pw, ph)
    pad = max(10, min(pw, ph) // 14)
    inner = Rect(px + pad, py + pad, pw - 2 * pad, ph - 2 * pad)
    left_w = int(inner.w * 0.56)
    diag_rect = Rect(inner.x, inner.y, left_w, inner.h)
    right_x = inner.x + left_w + pad
    right = Rect(right_x, inner.y, inner.x + inner.w - right_x, inner.h)
    return StatusOverlay(
        panel=panel, diag_rect=diag_rect, brightness=build_brightness_overlay(right)
    )


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
    if reading.session_percent is not None:
        s_reset, _ = fmt.format_countdown(reading.session_resets_at, now=now)
        lines.append(
            DetailLine("Session", fmt.percent_text(reading.session_percent),
                       fmt.bar_color(reading.session_percent))
        )
        if s_reset:
            lines.append(DetailLine("  resets in", s_reset, fmt.GRAY))
    if reading.weekly_percent is not None:
        w_reset, _ = fmt.format_countdown(reading.weekly_resets_at, now=now)
        lines.append(
            DetailLine("Weekly", fmt.percent_text(reading.weekly_percent),
                       fmt.bar_color(reading.weekly_percent))
        )
        if w_reset:
            lines.append(DetailLine("  resets in", w_reset, fmt.GRAY))
    for sl in reading.scoped_limits or []:
        lines.append(
            DetailLine(sl.name, fmt.percent_text(sl.percent), fmt.bar_color(sl.percent))
        )
        sl_reset, _ = fmt.format_countdown(sl.resets_at, now=now)
        if sl_reset:
            lines.append(DetailLine("  resets in", sl_reset, fmt.GRAY))
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
