from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import httpx

from usage_dashboard.server.api import create_app
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
            assert "CLAUDE" in response.text

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

    def test_dashboard_shows_qwen_offpeak_card(self, tmp_path):
        # Display-only QWEN tag: always present, coloured green/orange by whether
        # the Qwen token plan's off-peak window (22:00–08:00 UTC+8) is open.
        app, db = _create_app_with_db(tmp_path)
        db.store_reading(_make_reading())

        async def _test():
            async with _client(app) as client:
                response = await client.get("/dashboard")
            assert ">QWEN</h2>" in response.text
            assert "color:#22c55e" in response.text or "color:#f97316" in response.text

        asyncio.run(_test())

    def test_dashboard_zai_header_tinted_by_offpeak(self, tmp_path):
        app, db = _create_app_with_db(tmp_path)
        db.store_reading(_make_reading(provider=Provider.ZAI))

        async def _test():
            async with _client(app) as client:
                response = await client.get("/dashboard")
            assert "<h2 style=\"color:" in response.text
            assert ">ZAI</h2>" in response.text

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
            assert text.count("<h2>CLAUDE") == 1
            assert "CLAUDE_WORK" not in text
            assert "me Session" in text and "work Session" in text

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
            assert "CLAUDE" in response.text
            assert "ZAI" not in response.text
            assert "OLLAMA" not in response.text
            assert "CODEX" not in response.text

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
