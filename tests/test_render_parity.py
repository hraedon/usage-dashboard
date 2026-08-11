"""The Pi panel and the web /dashboard must not disagree about what to show.

They are two hand-written renderers over one model (``Reading`` -> bars +
hints) with no shared presentation layer, so every new display hint is an
opportunity for silent divergence — and the gap is only ever found by a human
looking at both screens. It has happened twice:

* WI-020 — the Fable scoped-limit bar rendered on the Pi, missing from the web.
* WI-030 — the z.ai peak countdown rendered on the Pi, missing from the web.

Note the direction is consistent: the Pi is the primary surface and gets the
feature; the web view lags. These tests assert the web view carries every hint
the panel does, for one fixed reading set and clock.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from usage_dashboard.client.layout import build_main_layout
from usage_dashboard.server.api import _render_dashboard_html
from usage_dashboard.shared.models import (
    Provider,
    Reading,
    ReadingStatus,
    ScopedLimit,
)
from usage_dashboard.shared.offpeak import qwen_peak_label, zai_peak_label

# A Wednesday. 06:00 UTC is 14:00 UTC+8 — inside z.ai's Mon–Fri 14:00–18:00
# peak window — so the "ends in" branch is exercised, not just "peak in".
_IN_PEAK = datetime(2026, 8, 12, 6, 30, tzinfo=timezone.utc)
# 02:00 UTC is 10:00 UTC+8: outside z.ai's window, inside Qwen's peak.
_OFF_PEAK = datetime(2026, 8, 12, 2, 0, tzinfo=timezone.utc)
_SIZE = (1280, 720)


def _reading(provider: Provider, now: datetime, **over: object) -> Reading:
    base: dict = {
        "provider": provider,
        "status": ReadingStatus.CURRENT,
        "session_percent": 50.0,
        "session_resets_at": now + timedelta(hours=2),
        "weekly_percent": 60.0,
        "weekly_resets_at": now + timedelta(days=2),
        "fetched_at": now.replace(tzinfo=None),
        "stale": False,
    }
    base.update(over)
    return Reading(**base)  # type: ignore[arg-type]


def _fleet(now: datetime) -> list[Reading]:
    return [
        _reading(
            Provider.CLAUDE, now,
            scoped_limits=[ScopedLimit(
                name="Fable", percent=13.0,
                resets_at=now + timedelta(days=3), is_active=False,
            )],
        ),
        _reading(Provider.CODEX, now, session_percent=None, session_resets_at=None),
        _reading(Provider.ZAI, now, detail="week req 1568 tok 183.0M"),
        _reading(Provider.OLLAMA, now),
    ]


class TestPeakCountdownParity:
    """WI-030: the tint answers 'is it peak?'; only the countdown answers
    'for how much longer?', which is the half you act on."""

    def test_web_shows_the_zai_peak_countdown_in_peak(self) -> None:
        html_out = _render_dashboard_html(_fleet(_IN_PEAK), _IN_PEAK)
        label = zai_peak_label(_IN_PEAK)
        assert label.startswith("ends in "), f"fixture drifted: {label!r}"
        assert label in html_out, (
            f"web /dashboard is missing the z.ai peak countdown {label!r}"
        )

    def test_web_shows_the_zai_peak_countdown_off_peak(self) -> None:
        html_out = _render_dashboard_html(_fleet(_OFF_PEAK), _OFF_PEAK)
        label = zai_peak_label(_OFF_PEAK)
        assert label.startswith("peak in "), f"fixture drifted: {label!r}"
        assert label in html_out

    def test_panel_and_web_use_the_same_zai_wording(self) -> None:
        """Both surfaces must render the identical string, so a change to the
        wording cannot land on one and not the other."""
        for now in (_IN_PEAK, _OFF_PEAK):
            layout = build_main_layout(_fleet(now), _SIZE, now=now)
            zai = next(t for t in layout.tiles if t.provider is Provider.ZAI)
            assert zai.subtitle == zai_peak_label(now)
            assert zai.subtitle in _render_dashboard_html(_fleet(now), now)

    def test_panel_and_web_use_the_same_qwen_wording(self) -> None:
        for now in (_IN_PEAK, _OFF_PEAK):
            layout = build_main_layout(_fleet(now), _SIZE, now=now)
            html_out = _render_dashboard_html(_fleet(now), now)
            label = qwen_peak_label(now)
            # The panel prefixes the provider name; the web card's header
            # already says QWEN. The countdown text itself must match.
            assert layout.footer_note == f"QWEN {label}"
            assert label in html_out


class TestScopedLimitParity:
    """WI-020: the Fable bar rendered on the Pi and was missing from the web."""

    def test_web_shows_every_scoped_limit_the_panel_shows(self) -> None:
        now = _OFF_PEAK
        layout = build_main_layout(_fleet(now), _SIZE, now=now)
        html_out = _render_dashboard_html(_fleet(now), now)
        claude = next(t for t in layout.tiles if t.provider is Provider.CLAUDE)
        scoped = [b.label for b in claude.bars if b.label not in ("Session", "Weekly")]
        assert scoped, "fixture no longer exercises a scoped limit"
        for label in scoped:
            assert label in html_out, (
                f"web /dashboard is missing the {label!r} bar the panel renders"
            )


class TestDetailParity:
    def test_web_shows_the_zai_detail_line(self) -> None:
        now = _OFF_PEAK
        html_out = _render_dashboard_html(_fleet(now), now)
        assert "week req 1568 tok 183.0M" in html_out
