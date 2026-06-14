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

from usage_dashboard.client.gui import DashboardGui  # noqa: E402
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


def test_smaller_resolution_renders(gui) -> None:
    layout = build_main_layout(_readings(), (240, 320))
    gui._width, gui._height = 240, 320
    gui._draw_main(layout)
