from __future__ import annotations

from datetime import datetime, timedelta, timezone

from PIL import Image

from usage_dashboard.client.renderer import DisplayRenderer, _bar_color, _format_countdown
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus


def _make_reading(**overrides: object) -> Reading:
    defaults = {
        "provider": Provider.CLAUDE,
        "status": ReadingStatus.CURRENT,
        "session_percent": 50.0,
        "session_resets_at": datetime(2026, 1, 15, 10, 0, 0),
        "weekly_percent": 60.0,
        "weekly_resets_at": datetime(2026, 1, 19, 0, 0, 0),
        "fetched_at": datetime(2026, 1, 14, 12, 0, 0),
        "stale": False,
    }
    defaults.update(overrides)
    return Reading(**defaults)  # type: ignore[arg-type]


class TestBarColor:
    def test_below_75_is_green(self):
        assert _bar_color(50.0) == (0x22, 0xC5, 0x5E)

    def test_exactly_75_is_orange(self):
        assert _bar_color(75.0) == (0xF9, 0x73, 0x16)

    def test_80_is_orange(self):
        assert _bar_color(80.0) == (0xF9, 0x73, 0x16)

    def test_exactly_85_is_red(self):
        assert _bar_color(85.0) == (0xEF, 0x44, 0x44)

    def test_90_is_red(self):
        assert _bar_color(90.0) == (0xEF, 0x44, 0x44)

    def test_none_is_gray(self):
        assert _bar_color(None) == (150, 150, 150)

    def test_74_9_is_green(self):
        assert _bar_color(74.9) == (0x22, 0xC5, 0x5E)

    def test_84_9_is_orange(self):
        assert _bar_color(84.9) == (0xF9, 0x73, 0x16)


class TestCountdownFormatting:
    def test_none_returns_empty(self):
        text, highlighted = _format_countdown(None)
        assert text == ""
        assert highlighted is False

    def test_past_time_returns_zero(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        text, highlighted = _format_countdown(past)
        assert text == "0m"
        assert highlighted is True

    def test_within_three_days_highlighted(self):
        target = datetime.now(timezone.utc) + timedelta(days=2)
        text, highlighted = _format_countdown(target)
        assert highlighted is True

    def test_beyond_three_days_not_highlighted(self):
        target = datetime.now(timezone.utc) + timedelta(days=5)
        text, highlighted = _format_countdown(target)
        assert highlighted is False

    def test_exactly_three_days_boundary(self):
        target = datetime.now(timezone.utc) + timedelta(days=3)
        text, highlighted = _format_countdown(target)
        assert highlighted is True

    def test_days_format(self):
        target = datetime.now(timezone.utc) + timedelta(days=4, hours=3)
        text, highlighted = _format_countdown(target)
        assert "d" in text
        assert "h" in text

    def test_hours_minutes_format(self):
        target = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
        text, highlighted = _format_countdown(target)
        assert "h" in text
        assert "m" in text


class TestDisplayRenderer:
    def test_render_produces_correct_dimensions(self):
        renderer = DisplayRenderer(width=240, height=320)
        readings = [_make_reading()]
        img = renderer.render(readings)
        assert img.size == (240, 320)

    def test_render_custom_dimensions(self):
        renderer = DisplayRenderer(width=200, height=400)
        readings = [_make_reading()]
        img = renderer.render(readings)
        assert img.size == (200, 400)

    def test_render_empty_readings_produces_image(self):
        renderer = DisplayRenderer()
        img = renderer.render([])
        assert isinstance(img, Image.Image)
        assert img.size == (240, 320)

    def test_render_with_offline_reading(self):
        renderer = DisplayRenderer()
        reading = _make_reading(
            status=ReadingStatus.OFFLINE,
            session_percent=None,
            weekly_percent=None,
            stale=True,
        )
        img = renderer.render([reading])
        assert isinstance(img, Image.Image)

    def test_render_multiple_providers(self):
        renderer = DisplayRenderer()
        readings = [
            _make_reading(provider=Provider.CLAUDE),
            _make_reading(provider=Provider.ZAI),
            _make_reading(provider=Provider.OLLAMA),
        ]
        img = renderer.render(readings)
        assert isinstance(img, Image.Image)

    def test_render_returns_rgb_image(self):
        renderer = DisplayRenderer()
        img = renderer.render([_make_reading()])
        assert img.mode == "RGB"

    def test_render_with_none_resets_at(self):
        renderer = DisplayRenderer()
        reading = _make_reading(session_resets_at=None, weekly_resets_at=None)
        img = renderer.render([reading])
        assert isinstance(img, Image.Image)
