from __future__ import annotations

import asyncio

import httpx

from usage_dashboard.server.api import create_app
from usage_dashboard.server.db import Database
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus

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
        # All four configured; only claude has a reading. The other three are
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
            assert providers == {
                "claude", "claude_work", "zai", "ollama", "codex", "umans",
            }

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

    def test_dashboard_shows_umans_detail_line(self, tmp_path):
        app, db = _create_app_with_db(tmp_path)
        db.store_reading(
            _make_reading(
                provider=Provider.UMANS,
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

    def test_dashboard_escapes_detail_content(self, tmp_path):
        app, db = _create_app_with_db(tmp_path)
        db.store_reading(
            _make_reading(provider=Provider.UMANS, detail="<script>alert(1)</script>")
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
            assert "UMANS" not in response.text

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
