from __future__ import annotations

from datetime import datetime, timezone

from PIL import Image, ImageDraw, ImageFont

from usage_dashboard.shared.models import Provider, Reading, ReadingStatus

_TILE_PROVIDER_ORDER: list[Provider] = [Provider.CLAUDE, Provider.ZAI, Provider.OLLAMA]

_BG = (0, 0, 0)
_TEXT = (255, 255, 255)
_GRAY = (150, 150, 150)
_GREEN = (0x22, 0xC5, 0x5E)
_ORANGE = (0xF9, 0x73, 0x16)
_RED = (0xEF, 0x44, 0x44)
_YELLOW = (0xEA, 0xB3, 0x08)
_BAR_BG = (50, 50, 50)

_THREE_DAYS_SECONDS = 3 * 24 * 3600


def _bar_color(percent: float | None) -> tuple[int, int, int]:
    if percent is None:
        return _GRAY
    if percent >= 85:
        return _RED
    if percent >= 75:
        return _ORANGE
    return _GREEN


def _format_countdown(resets_at: datetime | None) -> tuple[str, bool]:
    if resets_at is None:
        return ("", False)
    now = datetime.now(timezone.utc)
    if resets_at.tzinfo is None:
        target = resets_at.replace(tzinfo=timezone.utc)
    else:
        target = resets_at.astimezone(timezone.utc)
    delta = target - now
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return ("0m", True)
    within_threshold = total_seconds <= _THREE_DAYS_SECONDS
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    if days > 0:
        return (f"{days}d {hours}h", within_threshold)
    return (f"{hours}h {minutes}m", within_threshold)


class DisplayRenderer:
    def __init__(self, width: int = 240, height: int = 320) -> None:
        self._width = width
        self._height = height
        self._font = ImageFont.load_default()
        self._bold_font = ImageFont.load_default()

    def render(self, readings: list[Reading]) -> Image.Image:
        img = Image.new("RGB", (self._width, self._height), _BG)
        draw = ImageDraw.Draw(img)

        readings_by_provider = {r.provider: r for r in readings}
        ordered = [
            readings_by_provider[p] for p in _TILE_PROVIDER_ORDER if p in readings_by_provider
        ]

        tile_height = 92
        padding = 6
        y = padding

        for reading in ordered:
            self._draw_tile(draw, reading, y, tile_height)
            y += tile_height + padding

        umans = readings_by_provider.get(Provider.UMANS)
        if umans is not None:
            self._draw_detail_line(draw, umans, y)

        return img

    def _draw_tile(self, draw: ImageDraw.ImageDraw, reading: Reading, y: int, height: int) -> None:
        x = 10
        title = reading.provider.value.upper()
        if reading.status == ReadingStatus.STALE or reading.stale:
            title += " [stale]"
        elif reading.status == ReadingStatus.OFFLINE:
            title += " [offline]"
        draw.text((x, y + 4), title, fill=_TEXT, font=self._bold_font)

        bar_y = y + 24
        self._draw_progress_row(draw, "Session", reading.session_percent, x, bar_y)
        self._draw_reset_countdown(draw, reading.session_resets_at, x, bar_y + 22, False)

        weekly_y = bar_y + 36
        self._draw_progress_row(draw, "Weekly", reading.weekly_percent, x, weekly_y)
        self._draw_reset_countdown(draw, reading.weekly_resets_at, x, weekly_y + 22, True)

    def _draw_detail_line(self, draw: ImageDraw.ImageDraw, reading: Reading, y: int) -> None:
        x = 10
        title = reading.provider.value.upper()
        if reading.status == ReadingStatus.STALE or reading.stale:
            title += " [stale]"
        elif reading.status == ReadingStatus.OFFLINE:
            title += " [offline]"
        draw.text((x, y), title, fill=_TEXT, font=self._bold_font)
        if reading.detail:
            detail_x = x + int(draw.textlength(title, font=self._bold_font)) + 8
            draw.text((detail_x, y), reading.detail, fill=_GRAY, font=self._font)

    def _draw_progress_row(
        self,
        draw: ImageDraw.ImageDraw,
        label: str,
        percent: float | None,
        x: int,
        y: int,
    ) -> None:
        draw.text((x, y), label, fill=_TEXT, font=self._font)

        bar_x = x + 52
        bar_width = 120
        bar_height = 10

        draw.rectangle([bar_x, y + 2, bar_x + bar_width, y + 2 + bar_height], fill=_BAR_BG)

        if percent is not None:
            fill_width = max(1, int(bar_width * min(percent, 100.0) / 100.0))
            color = _bar_color(percent)
            draw.rectangle([bar_x, y + 2, bar_x + fill_width, y + 2 + bar_height], fill=color)

        pct_x = bar_x + bar_width + 4
        pct_text = f"{percent:.0f}%" if percent is not None else "N/A"
        draw.text((pct_x, y), pct_text, fill=_TEXT, font=self._font)

    def _draw_reset_countdown(
        self,
        draw: ImageDraw.ImageDraw,
        resets_at: datetime | None,
        x: int,
        y: int,
        is_weekly: bool,
    ) -> None:
        text, highlighted = _format_countdown(resets_at)
        if not text:
            return
        color = _YELLOW if highlighted else _GRAY
        draw.text((x + 52, y), f"Resets: {text}", fill=color, font=self._font)
