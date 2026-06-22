from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from usage_dashboard.server.api import create_app
from usage_dashboard.server.db import Database
from usage_dashboard.server.schedule_config import ScheduleConfig

API_KEY = "test-secret-key"


def _write(base: Path, name: str, spec: str) -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / name).write_text(spec)


class TestScheduleConfigLoad:
    def test_missing_dir_is_empty(self, tmp_path: Path) -> None:
        cfg = ScheduleConfig.load(tmp_path / "nope")
        assert cfg.for_unit("mpmusage01") is None
        assert cfg.for_unit(None) is None

    def test_unit_falls_back_to_default(self, tmp_path: Path) -> None:
        _write(tmp_path, "default", "daily 00:00-08:00")
        _write(tmp_path, "mpmusage02", "daily 00:00-08:00; sat 00:00-sun 23:59")
        cfg = ScheduleConfig.load(tmp_path)
        assert cfg.for_unit("mpmusage02") == "daily 00:00-08:00; sat 00:00-sun 23:59"
        assert cfg.for_unit("mpmusage01") == "daily 00:00-08:00"  # default
        assert cfg.for_unit(None) == "daily 00:00-08:00"

    def test_dotfiles_and_blank_entries_skipped(self, tmp_path: Path) -> None:
        _write(tmp_path, "default", "daily 00:00-08:00")
        _write(tmp_path, "..data", "ignored")   # ConfigMap internal link name
        _write(tmp_path, "blank", "   ")
        cfg = ScheduleConfig.load(tmp_path)
        assert cfg.for_unit("blank") == "daily 00:00-08:00"  # blank ignored -> default
        assert cfg.for_unit(None) == "daily 00:00-08:00"


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class TestScheduleEndpoint:
    def _app(self, tmp_path: Path, cfg: ScheduleConfig | None):
        db = Database(str(tmp_path / "api.db"))
        db.initialize()
        return create_app(API_KEY, db, schedule_config=cfg)

    def test_returns_unit_spec(self, tmp_path: Path) -> None:
        _write(tmp_path / "sch", "default", "daily 00:00-08:00")
        _write(tmp_path / "sch", "mpmusage02", "daily 01:00-09:00")
        app = self._app(tmp_path, ScheduleConfig.load(tmp_path / "sch"))

        async def _test():
            async with _client(app) as c:
                r = await c.get("/schedule?unit=mpmusage02",
                                headers={"Authorization": f"Bearer {API_KEY}"})
                assert r.status_code == 200
                assert r.json() == {"schedule": "daily 01:00-09:00"}
                # unknown unit -> default
                r2 = await c.get("/schedule?unit=unknown",
                                 headers={"Authorization": f"Bearer {API_KEY}"})
                assert r2.json() == {"schedule": "daily 00:00-08:00"}

        asyncio.run(_test())

    def test_requires_auth(self, tmp_path: Path) -> None:
        app = self._app(tmp_path, ScheduleConfig({"default": "daily 00:00-08:00"}))

        async def _test():
            async with _client(app) as c:
                assert (await c.get("/schedule")).status_code == 401

        asyncio.run(_test())

    def test_null_when_no_config(self, tmp_path: Path) -> None:
        app = self._app(tmp_path, None)

        async def _test():
            async with _client(app) as c:
                r = await c.get("/schedule",
                                headers={"Authorization": f"Bearer {API_KEY}"})
                assert r.status_code == 200
                assert r.json() == {"schedule": None}

        asyncio.run(_test())
