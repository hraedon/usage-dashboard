from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import httpx

from usage_dashboard.server.api import (
    _WEB_BODY_PADDING,
    _WEB_GRID_GAP,
    _WEB_GRID_MIN_WIDTH,
    _WEB_MAX_WIDTH,
    _render_dashboard_html,
    create_app,
)
from usage_dashboard.server.db import Database
from usage_dashboard.shared.models import ALERT_CRIT, ALERT_WARN, Provider, Reading, ReadingStatus

API_KEY = "test-secret-key"


def _make_reading(**overrides: object) -> Reading:
    from datetime import datetime

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


def _create_app_with_db(tmp_path, configured_providers=None):
    db = Database(str(tmp_path / "api_test.db"))
    db.initialize()
    app = create_app(API_KEY, db, configured_providers=configured_providers)
    return app, db


def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


class TestReadingsEndpoint:
    def test_get_readings_with_valid_api_key_returns_200(self, tmp_path):
        app, db = _create_app_with_db(tmp_path)
        reading = _make_reading()
        db.store_reading(reading)

        async def _test():
            async with _client(app) as client:
                response = await client.get(
                    "/readings",
                    headers={"Authorization": f"Bearer {API_KEY}"},
                )
            assert response.status_code == 200
            data = response.json()
            assert isinstance(data, list)
            providers = [item["provider"] for item in data]
            assert "claude" in providers

        asyncio.run(_test())

    def test_get_readings_without_auth_returns_401(self, tmp_path):
        app, _ = _create_app_with_db(tmp_path)

        async def _test():
            async with _client(app) as client:
                response = await client.get("/readings")
            assert response.status_code == 401

        asyncio.run(_test())

    def test_get_readings_with_wrong_api_key_returns_401(self, tmp_path):
        app, _ = _create_app_with_db(tmp_path)

        async def _test():
            async with _client(app) as client:
                response = await client.get(
                    "/readings",
                    headers={"Authorization": "Bearer wrong-key"},
                )
            assert response.status_code == 401

        asyncio.run(_test())

    def test_get_readings_returns_all_configured_providers(self, tmp_path):
        # Every provider configured; only claude has a reading. The rest are
        # configured-but-not-reporting, so they legitimately show as offline.
        app, db = _create_app_with_db(tmp_path, configured_providers=list(Provider))
        claude = _make_reading(provider=Provider.CLAUDE)
        db.store_reading(claude)

        async def _test():
            async with _client(app) as client:
                response = await client.get(
                    "/readings",
                    headers={"Authorization": f"Bearer {API_KEY}"},
                )
            data = response.json()
            providers = {item["provider"] for item in data}
            # Derived from the enum rather than spelled out: the point of the
            # test is "every configured provider is reported", and a literal
            # list only re-states the fixture while going stale on each new
            # provider.
            assert providers == {p.value for p in Provider}

        asyncio.run(_test())

    def test_configured_but_not_reporting_providers_show_as_offline(self, tmp_path):
        app, db = _create_app_with_db(tmp_path, configured_providers=list(Provider))

        async def _test():
            async with _client(app) as client:
                response = await client.get(
                    "/readings",
                    headers={"Authorization": f"Bearer {API_KEY}"},
                )
            data = response.json()
            for item in data:
                if item["provider"] != "claude":
                    assert item["status"] == "offline"
                    assert item["session_percent"] is None
                    assert item["stale"] is True

        asyncio.run(_test())

    def test_unconfigured_providers_are_omitted(self, tmp_path):
        # WI-003: a provider that was never configured must not appear at all,
        # so a real outage is distinguishable from an absent config.
        app, db = _create_app_with_db(
            tmp_path, configured_providers=[Provider.CLAUDE]
        )
        db.store_reading(_make_reading(provider=Provider.CLAUDE))

        async def _test():
            async with _client(app) as client:
                response = await client.get(
                    "/readings",
                    headers={"Authorization": f"Bearer {API_KEY}"},
                )
            data = response.json()
            providers = {item["provider"] for item in data}
            assert providers == {"claude"}

        asyncio.run(_test())

    def test_readings_response_has_expected_fields(self, tmp_path):
        app, db = _create_app_with_db(tmp_path)
        reading = _make_reading()
        db.store_reading(reading)

        async def _test():
            async with _client(app) as client:
                response = await client.get(
                    "/readings",
                    headers={"Authorization": f"Bearer {API_KEY}"},
                )
            data = response.json()
            item = next(i for i in data if i["provider"] == "claude")
            assert "status" in item
            assert "session_percent" in item
            assert "session_resets_at" in item
            assert "weekly_percent" in item
            assert "weekly_resets_at" in item
            assert "fetched_at" in item
            assert "stale" in item

        asyncio.run(_test())


class TestHealthEndpoint:
    def test_health_returns_200(self, tmp_path):
        app, _ = _create_app_with_db(tmp_path)

        async def _test():
            async with _client(app) as client:
                response = await client.get("/health")
            assert response.status_code == 200

        asyncio.run(_test())

    def test_health_returns_ok(self, tmp_path):
        app, _ = _create_app_with_db(tmp_path)

        async def _test():
            async with _client(app) as client:
                response = await client.get("/health")
            assert response.json() == {"status": "ok"}

        asyncio.run(_test())

    def test_health_no_auth_required(self, tmp_path):
        app, _ = _create_app_with_db(tmp_path)

        async def _test():
            async with _client(app) as client:
                response = await client.get("/health")
            assert response.status_code == 200

        asyncio.run(_test())


class TestDashboardEndpoint:
    def test_dashboard_requires_no_auth(self, tmp_path):
        app, db = _create_app_with_db(tmp_path)
        db.store_reading(_make_reading())

        async def _test():
            async with _client(app) as client:
                response = await client.get("/dashboard")
            assert response.status_code == 200
            assert "text/html" in response.headers["content-type"]
            assert "Claude" in response.text

        asyncio.run(_test())

    def test_dashboard_shows_quotaless_detail_line(self, tmp_path):
        app, db = _create_app_with_db(tmp_path)
        db.store_reading(
            _make_reading(
                provider=Provider.ZAI,
                session_percent=None,
                weekly_percent=None,
                weekly_resets_at=None,
                detail="req 161  tok 63.9M",
            )
        )

        async def _test():
            async with _client(app) as client:
                response = await client.get("/dashboard")
            assert "req 161  tok 63.9M" in response.text

        asyncio.run(_test())

    def test_dashboard_shows_zai_weekly_token_line(self, tmp_path):
        # z.ai's weekly-window token total renders under the percentage bars,
        # coloured by the volume alert (warn = orange).
        app, db = _create_app_with_db(tmp_path)
        db.store_reading(
            _make_reading(
                provider=Provider.ZAI,
                detail="week req 2273  tok 284.0M",
                alert=ALERT_WARN,
            )
        )

        async def _test():
            async with _client(app) as client:
                response = await client.get("/dashboard")
            assert "week req 2273  tok 284.0M" in response.text
            assert 'color:#f97316' in response.text  # warn orange

        asyncio.run(_test())

    def test_dashboard_zai_alert_crit_colors_detail_red(self, tmp_path):
        app, db = _create_app_with_db(tmp_path)
        db.store_reading(
            _make_reading(
                provider=Provider.ZAI,
                detail="week req 2273  tok 290.0M",
                alert=ALERT_CRIT,
            )
        )

        async def _test():
            async with _client(app) as client:
                response = await client.get("/dashboard")
            assert "week req 2273  tok 290.0M" in response.text
            assert 'color:#ef4444' in response.text  # crit red

        asyncio.run(_test())

    def test_dashboard_has_no_extra_plan_card(self, tmp_path):
        app, db = _create_app_with_db(
            tmp_path, configured_providers=[Provider.CLAUDE]
        )
        db.store_reading(_make_reading())

        async def _test():
            async with _client(app) as client:
                response = await client.get("/dashboard")
            assert response.text.count('class="card status-current"') == 1

        asyncio.run(_test())

    def test_dashboard_keeps_opencode_web_card(self, tmp_path):
        app, db = _create_app_with_db(
            tmp_path, configured_providers=[Provider.OPENCODE]
        )
        db.store_reading(_make_reading(provider=Provider.OPENCODE))

        async def _test():
            async with _client(app) as client:
                response = await client.get("/dashboard")
            assert response.text.count('class="card status-current"') == 1
            assert ">OpenCode Go<" in response.text

        asyncio.run(_test())

    def test_dashboard_zai_header_tinted_by_offpeak(self, tmp_path):
        app, db = _create_app_with_db(tmp_path)
        db.store_reading(_make_reading(provider=Provider.ZAI))

        async def _test():
            async with _client(app) as client:
                response = await client.get("/dashboard")
            assert "<h2 style=\"color:" in response.text
            # The heading now carries the peak countdown as a trailing span
            # (WI-030), so ZAI is no longer the whole element body.
            assert ">ZAI<span" in response.text
            assert 'class="peak"' in response.text

        asyncio.run(_test())

    def test_root_redirects_to_dashboard(self, tmp_path):
        app, _ = _create_app_with_db(tmp_path)

        async def _test():
            async with _client(app) as client:
                response = await client.get("/")  # no auth, no follow
            assert response.status_code in (307, 308)
            assert response.headers["location"] == "/dashboard"

        asyncio.run(_test())

    def test_dashboard_is_responsive_grid(self, tmp_path):
        app, db = _create_app_with_db(tmp_path)
        db.store_reading(_make_reading(provider=Provider.CLAUDE))

        async def _test():
            async with _client(app) as client:
                response = await client.get("/dashboard")
            text = response.text
            assert 'name="viewport"' in text
            assert 'class="grid"' in text and "auto-fit" in text

        asyncio.run(_test())

    def test_dashboard_folds_work_account_into_claude_card(self, tmp_path):
        app, db = _create_app_with_db(
            tmp_path,
            configured_providers=[Provider.CLAUDE, Provider.CLAUDE_WORK],
        )
        db.store_reading(_make_reading(provider=Provider.CLAUDE))
        db.store_reading(_make_reading(provider=Provider.CLAUDE_WORK))

        async def _test():
            async with _client(app) as client:
                response = await client.get("/dashboard")
            text = response.text
            # One CLAUDE card with both accounts' rows; no CLAUDE_WORK header.
            assert text.count("<h2>Claude") == 1
            assert "CLAUDE_WORK" not in text
            assert "me Session" in text and "work Session" in text
            assert 'class="provider-count">1 provider</span>' in text

        asyncio.run(_test())

    def test_dashboard_count_keeps_opencode_while_folding_claude_work(self, tmp_path):
        app, db = _create_app_with_db(
            tmp_path,
            configured_providers=[
                Provider.CLAUDE,
                Provider.CLAUDE_WORK,
                Provider.OPENCODE,
            ],
        )
        db.store_reading(_make_reading(provider=Provider.CLAUDE))
        db.store_reading(_make_reading(provider=Provider.CLAUDE_WORK))
        db.store_reading(_make_reading(provider=Provider.OPENCODE))

        async def _test():
            async with _client(app) as client:
                response = await client.get("/dashboard")
            assert 'class="provider-count">2 providers</span>' in response.text
            assert ">OpenCode Go<" in response.text

        asyncio.run(_test())

    def test_dashboard_shows_scoped_limit_rows(self, tmp_path):
        from datetime import datetime

        from usage_dashboard.shared.models import ScopedLimit

        app, db = _create_app_with_db(tmp_path)
        db.store_reading(
            _make_reading(
                scoped_limits=[
                    ScopedLimit(
                        name="Fable",
                        percent=13.0,
                        resets_at=datetime(2026, 1, 18, 0, 0, 0),
                    )
                ],
            )
        )

        async def _test():
            async with _client(app) as client:
                response = await client.get("/dashboard")
            assert "Fable" in response.text
            assert "13%" in response.text

        asyncio.run(_test())

    def test_dashboard_skips_none_percent_bars(self, tmp_path):
        # Weekly-only Codex (session_percent=None) should show only the
        # Weekly bar on the dashboard, not a grayed "N/A" Session bar.
        app, db = _create_app_with_db(
            tmp_path, configured_providers=[Provider.CODEX]
        )
        db.store_reading(
            _make_reading(
                provider=Provider.CODEX,
                session_percent=None,
                session_resets_at=None,
                weekly_percent=55.0,
            )
        )

        async def _test():
            async with _client(app) as client:
                response = await client.get("/dashboard")
            assert "Weekly" in response.text
            assert "55%" in response.text
            assert "Session" not in response.text

        asyncio.run(_test())

    def test_dashboard_escapes_detail_content(self, tmp_path):
        app, db = _create_app_with_db(tmp_path)
        db.store_reading(
            _make_reading(
                provider=Provider.ZAI,
                session_percent=None,
                weekly_percent=None,
                weekly_resets_at=None,
                detail="<script>alert(1)</script>",
            )
        )

        async def _test():
            async with _client(app) as client:
                response = await client.get("/dashboard")
            assert "<script>" not in response.text
            assert "&lt;script&gt;" in response.text

        asyncio.run(_test())

    def test_dashboard_marks_configured_providers_offline(self, tmp_path):
        app, _db = _create_app_with_db(
            tmp_path, configured_providers=list(Provider)
        )

        async def _test():
            async with _client(app) as client:
                response = await client.get("/dashboard")
            assert response.status_code == 200
            assert response.text.count("offline") >= 4

        asyncio.run(_test())

    def test_dashboard_omits_unconfigured_providers(self, tmp_path):
        # WI-003: unconfigured providers must not render at all on /dashboard.
        app, db = _create_app_with_db(
            tmp_path, configured_providers=[Provider.CLAUDE]
        )
        db.store_reading(_make_reading(provider=Provider.CLAUDE))

        async def _test():
            async with _client(app) as client:
                response = await client.get("/dashboard")
            assert response.status_code == 200
            assert "Claude" in response.text
            assert "ZAI" not in response.text
            assert "OLLAMA" not in response.text
            assert "CODEX" not in response.text

        asyncio.run(_test())

    def test_dashboard_has_stable_order_human_names_and_folded_count(self, tmp_path):
        configured = [
            Provider.OPENCODE,
            Provider.OLLAMA,
            Provider.CLAUDE_WORK,
            Provider.ZAI,
            Provider.CODEX,
            Provider.CLAUDE,
        ]
        app, db = _create_app_with_db(tmp_path, configured_providers=configured)
        for provider in configured:
            db.store_reading(_make_reading(provider=provider))

        async def _test():
            async with _client(app) as client:
                response = await client.get("/dashboard")
            text = response.text
            providers = re.findall(
                r'<section class="card status-\w+" data-provider="([^"]+)"',
                text,
            )
            # Drop the trailing spans (status badge, z.ai peak countdown)
            # element-and-all, then strip any remaining tags. Splitting on the
            # word "peak" instead would be wall-clock dependent: the countdown
            # reads "peak in 3h 24m" off-peak but "ends in 1h 24m" during peak
            # (Mon-Fri 14:00-18:00 SGT), so a name-only assertion built on that
            # word passes or fails depending on when CI happens to run.
            names = [
                re.sub(r"<[^>]+>", "", re.sub(r"<span\b[^>]*>.*?</span>", "", heading))
                for heading in re.findall(r"<h2[^>]*>(.*?)</h2>", text)
            ]
            assert providers == ["claude", "codex", "zai", "ollama", "opencode"]
            assert [name.strip() for name in names] == [
                "Claude",
                "Codex",
                "ZAI",
                "Ollama",
                "OpenCode Go",
            ]
            # Six readings produce five visible cards because the two Claude
            # accounts intentionally share one card.
            assert 'class="provider-count">5 providers</span>' in text
            assert "OpenCode Go" in text

        asyncio.run(_test())

    def test_dashboard_shows_work_only_as_one_claude_card(self, tmp_path):
        app, db = _create_app_with_db(
            tmp_path, configured_providers=[Provider.CLAUDE_WORK]
        )
        db.store_reading(_make_reading(provider=Provider.CLAUDE_WORK))

        async def _test():
            async with _client(app) as client:
                response = await client.get("/dashboard")
            assert response.text.count('data-provider="claude"') == 1
            assert ">Claude<" in response.text
            assert 'class="provider-count">1 provider</span>' in response.text

        asyncio.run(_test())

    def test_dashboard_reset_states_are_present_in_html(self, tmp_path):
        now = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
        reading = _make_reading(
            session_resets_at=now - timedelta(minutes=1),
            weekly_resets_at=now + timedelta(days=1),
        )
        html_out = _render_dashboard_html([reading], now)

        assert 'class="resets reset-expired" data-reset-state="expired"' in html_out
        assert 'class="resets reset-near" data-reset-state="near"' in html_out
        assert 'data-reset-state="distant"' not in html_out

    def test_dashboard_statuses_have_distinct_glanceable_styles(self):
        now = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
        html_out = _render_dashboard_html(
            [
                _make_reading(
                    provider=Provider.ZAI,
                    status=ReadingStatus.STALE,
                    stale=True,
                ),
                _make_reading(
                    provider=Provider.OLLAMA,
                    status=ReadingStatus.OFFLINE,
                    stale=True,
                ),
            ],
            now,
        )

        assert 'data-provider="zai" data-status="stale"' in html_out
        assert '<span class="badge badge-stale">stale</span>' in html_out
        assert 'data-provider="ollama" data-status="offline"' in html_out
        assert '<span class="badge badge-offline">offline</span>' in html_out
        assert ".card.status-stale" in html_out
        assert ".card.status-offline" in html_out
        assert "#f97316" in html_out and "#ef4444" in html_out

    def test_dashboard_css_viewport_contract(self, tmp_path):
        app, db = _create_app_with_db(tmp_path, configured_providers=[Provider.CLAUDE])
        db.store_reading(_make_reading())

        async def _test():
            async with _client(app) as client:
                text = (await client.get("/dashboard")).text

            assert f"--maxw: {_WEB_MAX_WIDTH}px" in text
            assert (
                f"minmax(min(100%, {_WEB_GRID_MIN_WIDTH}px), 1fr)" in text
            )
            assert "min-width:0" in text

            def columns(viewport: int) -> int:
                content = min(viewport - 2 * _WEB_BODY_PADDING, _WEB_MAX_WIDTH)
                return max(
                    1,
                    (content + _WEB_GRID_GAP)
                    // (_WEB_GRID_MIN_WIDTH + _WEB_GRID_GAP),
                )

            assert columns(1280) == 4
            assert columns(1440) == 4
            assert columns(320) == 1
            assert columns(390) == 1

        asyncio.run(_test())

    def test_readings_still_requires_auth(self, tmp_path):
        app, _db = _create_app_with_db(tmp_path)

        async def _test():
            async with _client(app) as client:
                response = await client.get("/readings")
            assert response.status_code == 401

        asyncio.run(_test())

    def test_dashboard_never_contains_api_key(self, tmp_path):
        app, db = _create_app_with_db(tmp_path)
        db.store_reading(_make_reading())

        async def _test():
            async with _client(app) as client:
                response = await client.get("/dashboard")
            assert API_KEY not in response.text

        asyncio.run(_test())


class TestHistoryEndpoint:
    def _store_recent(self, db, **overrides):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        overrides.setdefault("fetched_at", now - timedelta(hours=1))
        reading = _make_reading(**overrides)
        db.store_reading(reading)
        return reading

    def test_history_with_valid_api_key_returns_200(self, tmp_path):
        app, db = _create_app_with_db(tmp_path)
        self._store_recent(db)

        async def _test():
            async with _client(app) as client:
                response = await client.get(
                    "/history?provider=claude",
                    headers={"Authorization": f"Bearer {API_KEY}"},
                )
            assert response.status_code == 200
            data = response.json()
            assert data["provider"] == "claude"
            assert data["hours"] == 24.0
            assert len(data["readings"]) == 1
            assert data["readings"][0]["provider"] == "claude"

        asyncio.run(_test())

    def test_history_oldest_first(self, tmp_path):
        from datetime import datetime, timedelta, timezone

        app, db = _create_app_with_db(tmp_path)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        self._store_recent(db, session_percent=70.0, fetched_at=now - timedelta(hours=1))
        self._store_recent(db, session_percent=30.0, fetched_at=now - timedelta(hours=3))

        async def _test():
            async with _client(app) as client:
                response = await client.get(
                    "/history?provider=claude&hours=24",
                    headers={"Authorization": f"Bearer {API_KEY}"},
                )
            data = response.json()
            assert [r["session_percent"] for r in data["readings"]] == [30.0, 70.0]

        asyncio.run(_test())

    def test_history_without_auth_returns_401(self, tmp_path):
        app, _ = _create_app_with_db(tmp_path)

        async def _test():
            async with _client(app) as client:
                response = await client.get("/history?provider=claude")
            assert response.status_code == 401

        asyncio.run(_test())

    def test_history_unknown_provider_returns_400(self, tmp_path):
        app, _ = _create_app_with_db(tmp_path)

        async def _test():
            async with _client(app) as client:
                response = await client.get(
                    "/history?provider=nosuch",
                    headers={"Authorization": f"Bearer {API_KEY}"},
                )
            assert response.status_code == 400

        asyncio.run(_test())

    def test_history_missing_provider_returns_422(self, tmp_path):
        app, _ = _create_app_with_db(tmp_path)

        async def _test():
            async with _client(app) as client:
                response = await client.get(
                    "/history",
                    headers={"Authorization": f"Bearer {API_KEY}"},
                )
            assert response.status_code == 422

        asyncio.run(_test())

    def test_history_hours_out_of_range_returns_400(self, tmp_path):
        app, _ = _create_app_with_db(tmp_path)

        async def _test():
            async with _client(app) as client:
                for bad in ("0", "-5", "10000"):
                    response = await client.get(
                        f"/history?provider=claude&hours={bad}",
                        headers={"Authorization": f"Bearer {API_KEY}"},
                    )
                    assert response.status_code == 400, bad

        asyncio.run(_test())

    def test_history_excludes_readings_outside_window(self, tmp_path):
        from datetime import datetime, timedelta, timezone

        app, db = _create_app_with_db(tmp_path)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        self._store_recent(db, fetched_at=now - timedelta(hours=1))
        self._store_recent(db, fetched_at=now - timedelta(hours=72))

        async def _test():
            async with _client(app) as client:
                response = await client.get(
                    "/history?provider=claude&hours=24",
                    headers={"Authorization": f"Bearer {API_KEY}"},
                )
            data = response.json()
            assert len(data["readings"]) == 1

        asyncio.run(_test())


class TestRefreshEndpoint:
    """Client-triggered on-demand refresh (WI-012): POST /refresh forces an
    immediate collection via scheduler.fetch_now, bearer-authenticated and
    rate-limited to one call per 60s per app instance."""

    @staticmethod
    def _app(tmp_path, scheduler=None):
        db = Database(str(tmp_path / "api_refresh.db"))
        db.initialize()
        return create_app(
            API_KEY, db,
            configured_providers=[Provider.CLAUDE],
            scheduler=scheduler,
        )

    def test_refresh_requires_auth(self, tmp_path):
        app = self._app(tmp_path, scheduler=MagicMock())

        async def _test():
            async with _client(app) as client:
                response = await client.post("/refresh")
            assert response.status_code == 401

        asyncio.run(_test())

    def test_refresh_with_wrong_api_key_returns_401(self, tmp_path):
        app = self._app(tmp_path, scheduler=MagicMock())

        async def _test():
            async with _client(app) as client:
                response = await client.post(
                    "/refresh",
                    headers={"Authorization": "Bearer wrong-key"},
                )
            assert response.status_code == 401

        asyncio.run(_test())

    def test_refresh_without_scheduler_returns_501(self, tmp_path):
        # An app built without a scheduler (tests / config-only) reports the
        # endpoint as unavailable rather than crashing on None.
        app = self._app(tmp_path, scheduler=None)

        async def _test():
            async with _client(app) as client:
                response = await client.post(
                    "/refresh",
                    headers={"Authorization": f"Bearer {API_KEY}"},
                )
            assert response.status_code == 501
            assert "no scheduler" in response.json()["detail"]

        asyncio.run(_test())

    def test_refresh_calls_fetch_now(self, tmp_path):
        scheduler = MagicMock()
        app = self._app(tmp_path, scheduler=scheduler)

        async def _test():
            async with _client(app) as client:
                response = await client.post(
                    "/refresh",
                    headers={"Authorization": f"Bearer {API_KEY}"},
                )
            assert response.status_code == 200
            assert response.json() == {"status": "ok", "refreshed": True}

        asyncio.run(_test())
        scheduler.fetch_now.assert_called_once()

    def test_refresh_rate_limits_second_request(self, tmp_path):
        scheduler = MagicMock()
        app = self._app(tmp_path, scheduler=scheduler)

        async def _test():
            async with _client(app) as client:
                headers = {"Authorization": f"Bearer {API_KEY}"}
                first = await client.post("/refresh", headers=headers)
                second = await client.post("/refresh", headers=headers)
            assert first.status_code == 200
            assert second.status_code == 429
            assert "retry in" in second.json()["detail"]

        asyncio.run(_test())
        scheduler.fetch_now.assert_called_once()  # the second call never ran

    def test_rate_limit_is_per_app_instance(self, tmp_path):
        # Each app gets an independent limiter: an immediate refresh on a
        # second app must not trip the first app's rate limit.
        scheduler_a = MagicMock()
        scheduler_b = MagicMock()
        app_a = self._app(tmp_path, scheduler=scheduler_a)
        app_b = self._app(tmp_path, scheduler=scheduler_b)

        async def _test():
            async with _client(app_a) as client:
                headers = {"Authorization": f"Bearer {API_KEY}"}
                await client.post("/refresh", headers=headers)
            async with _client(app_b) as client:
                response = await client.post(
                    "/refresh",
                    headers={"Authorization": f"Bearer {API_KEY}"},
                )
            assert response.status_code == 200

        asyncio.run(_test())


class TestVersionedApiSurface:
    """Plan 003 WP-2: authenticated routes move under /api/v1, legacy paths
    keep working as aliases until the fleet has rolled (WP-4).

    The server rolls on an image rebuild; the Pis roll on their own 15-minute
    timer. A flag-day rename would 404 every unit until it caught up — quietly,
    which is the exact failure this plan exists to end.
    """

    _AUTHED = [("GET", "/readings"), ("GET", "/schedule"), ("GET", "/history")]

    def test_authed_routes_answer_on_both_path_sets(self, tmp_path):
        app, db = _create_app_with_db(tmp_path, configured_providers=[Provider.CLAUDE])
        db.store_reading(_make_reading(provider=Provider.CLAUDE))

        async def _test():
            async with _client(app) as client:
                for method, path in self._AUTHED:
                    for full in (path, f"/api/v1{path}"):
                        r = await client.request(
                            method, full,
                            params={"provider": "claude", "hours": 1},
                            headers={"Authorization": f"Bearer {API_KEY}"},
                        )
                        assert r.status_code == 200, f"{method} {full} -> {r.status_code}"

        asyncio.run(_test())

    def test_both_path_sets_return_identical_payloads(self, tmp_path):
        # Mounted from one router, so they cannot diverge — assert it, because
        # a future refactor that re-declares the alias separately could.
        app, db = _create_app_with_db(tmp_path, configured_providers=[Provider.CLAUDE])
        db.store_reading(_make_reading(provider=Provider.CLAUDE))

        async def _test():
            async with _client(app) as client:
                h = {"Authorization": f"Bearer {API_KEY}"}
                legacy = await client.get("/readings", headers=h)
                versioned = await client.get("/api/v1/readings", headers=h)
                assert legacy.json() == versioned.json()

        asyncio.run(_test())

    def test_versioned_routes_still_require_auth(self, tmp_path):
        # The /api prefix is routed externally as a single rule, so anything
        # under it that forgot its dependency would be internet-reachable.
        app, _db = _create_app_with_db(tmp_path)

        async def _test():
            async with _client(app) as client:
                for _method, path in self._AUTHED:
                    r = await client.get(f"/api/v1{path}")
                    assert r.status_code == 401, f"/api/v1{path} -> {r.status_code}"
                r = await client.post("/api/v1/refresh")
                assert r.status_code == 401

        asyncio.run(_test())

    def test_no_unauthenticated_view_is_reachable_under_api(self, tmp_path):
        # The whole /api prefix is public-facing. /dashboard is deliberately
        # unauthenticated for the private network; it must not acquire an /api
        # alias by accident.
        app, _db = _create_app_with_db(tmp_path)

        async def _test():
            async with _client(app) as client:
                for path in ("/api/v1/dashboard", "/api/dashboard", "/api/v1/",
                             "/api/v1/health", "/api/health", "/api"):
                    r = await client.get(path)
                    assert r.status_code == 404, f"{path} -> {r.status_code}"

        asyncio.run(_test())

    def test_every_authed_route_has_a_versioned_twin(self, tmp_path):
        # Derived from the app, so a route added to only one mount is caught
        # rather than depending on this test's hardcoded list staying current.
        #
        # Read from the OpenAPI schema, NOT `app.routes`: from fastapi 0.141
        # `include_router` leaves an opaque `_IncludedRouter` there instead of
        # flattened APIRoutes, so a route walk finds nothing and this test
        # would pass over an empty set. The schema is stable across versions.
        from usage_dashboard.server.api import API_V1_PREFIX
        app, _db = _create_app_with_db(tmp_path)
        paths = set(app.openapi().get("paths") or {})
        versioned = {p for p in paths if p.startswith(API_V1_PREFIX)}
        legacy_of = {p[len(API_V1_PREFIX):] for p in versioned}
        assert legacy_of <= paths, (
            f"versioned routes with no legacy alias: {sorted(legacy_of - paths)}"
        )
        assert versioned, "no versioned routes registered at all"
