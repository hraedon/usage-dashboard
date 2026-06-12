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


def _create_app_with_db(tmp_path):
    db = Database(str(tmp_path / "api_test.db"))
    db.initialize()
    app = create_app(API_KEY, db)
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

    def test_get_readings_returns_all_providers(self, tmp_path):
        app, db = _create_app_with_db(tmp_path)
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
            assert providers == {"claude", "zai", "ollama", "umans"}

        asyncio.run(_test())

    def test_missing_providers_show_as_offline(self, tmp_path):
        app, db = _create_app_with_db(tmp_path)

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
