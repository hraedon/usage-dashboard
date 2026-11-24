"""The display credential (Plan 010) reads the readings route and nothing else.

The Pi cellular readings worker crosses a metered, firewalled link holding one
bearer token. That token must be the least privileged one the server issues: it
may fetch the stored readings snapshot, and it may not trigger a provider
refresh, read history, read a unit schedule, assess capacity, or reach the
enrolment pane. These tests pin that scope on both path sets — the versioned
`/api/v1` routes and the legacy root aliases, since the readings router is
mounted on both.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
import pytest

from usage_dashboard.server.api import EXPOSURE_EXTERNAL, EXPOSURE_KEY, create_app
from usage_dashboard.server.db import Database
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus

OPERATOR = "operator-test"
AGENT = "agent-test"
DISPLAY = "display-test"

READINGS_PATHS = ("/api/v1/readings", "/readings")

# Every other authenticated surface, on both path sets. The display credential
# must be refused on all of them.
REFUSED = [
    ("GET", "/schedule"),
    ("POST", "/refresh"),
    ("GET", "/history?provider=claude"),
    ("GET", "/api/v1/schedule"),
    ("POST", "/api/v1/refresh"),
    ("GET", "/api/v1/history?provider=claude"),
    ("GET", "/api/v1/agent/capacity"),
    ("GET", "/internal/v1/login/status"),
]


def _reading() -> Reading:
    fetched = datetime.now(timezone.utc).replace(tzinfo=None)
    return Reading(
        Provider.CLAUDE, ReadingStatus.CURRENT, 40.0, fetched, 55.0, fetched, fetched, False,
        detail="a provider diagnostic that only the readings route carries",
    )


@pytest.fixture
def api(tmp_path):
    db = Database(str(tmp_path / "display.db"))
    db.initialize()
    db.store_reading(_reading())
    scheduler = MagicMock()
    app = create_app(
        OPERATOR, db, configured_providers=[Provider.CLAUDE],
        scheduler=scheduler, agent_api_key=AGENT, display_api_key=DISPLAY,
    )
    return app, db, scheduler


def request(app, path, key=DISPLAY, method="GET"):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Authorization": "Bearer " + key} if key else {}
            return await client.request(method, path, headers=headers)
    return asyncio.run(run())


@pytest.mark.parametrize("path", READINGS_PATHS)
def test_display_token_reads_the_stored_snapshot(api, path):
    app, _db, scheduler = api
    response = request(app, path)
    assert response.status_code == 200
    assert [item["provider"] for item in response.json()] == ["claude"]
    # It is the full display projection, not the narrower capacity projection.
    detail = "a provider diagnostic that only the readings route carries"
    assert response.json()[0]["detail"] == detail
    scheduler.fetch_now.assert_not_called()


def test_display_and_operator_tokens_get_identical_readings(api):
    app, _db, _scheduler = api
    for path in READINGS_PATHS:
        assert request(app, path, key=DISPLAY).json() == request(app, path, key=OPERATOR).json()


@pytest.mark.parametrize("method,path", REFUSED)
def test_display_token_is_refused_on_every_other_authed_route(api, method, path):
    app, _db, scheduler = api
    response = request(app, path, method=method)
    assert response.status_code == 401, f"{method} {path} -> {response.status_code}"
    scheduler.fetch_now.assert_not_called()


def test_display_token_never_triggers_a_provider_refresh(api):
    app, _db, scheduler = api
    for path in ("/refresh", "/api/v1/refresh"):
        assert request(app, path, method="POST").status_code == 401
    assert scheduler.mock_calls == []


def test_agent_token_still_cannot_read_the_readings_route(api):
    # The display credential is an addition to the readings route only; the
    # agent credential's scope (Plan 008) is unchanged.
    app, _db, _scheduler = api
    for path in READINGS_PATHS:
        assert request(app, path, key=AGENT).status_code == 401


def test_operator_token_keeps_full_access(api):
    app, _db, _scheduler = api
    for path in READINGS_PATHS:
        assert request(app, path, key=OPERATOR).status_code == 200
    for _method, path in REFUSED:
        assert request(app, path, key=OPERATOR).status_code != 401, path


@pytest.mark.parametrize("key", [None, "wrong", AGENT])
def test_readings_route_still_requires_a_valid_token(api, key):
    app, _db, _scheduler = api
    for path in READINGS_PATHS:
        assert request(app, path, key=key).status_code == 401


def test_display_token_is_optional(api):
    app = create_app(OPERATOR, api[1], agent_api_key=AGENT)
    for path in READINGS_PATHS:
        assert request(app, path, key=DISPLAY).status_code == 401
        assert request(app, path, key=OPERATOR).status_code == 200


@pytest.mark.parametrize(
    "operator,agent,display",
    [
        ("same", None, "same"),           # == operator key
        (OPERATOR, "same", "same"),       # == agent key
        ("same", AGENT, "same"),          # == operator key, agent present
    ],
)
def test_display_key_must_differ_from_every_other_credential(api, operator, agent, display):
    with pytest.raises(ValueError, match="must differ"):
        create_app(operator, api[1], agent_api_key=agent, display_api_key=display)


def test_openapi_keeps_readings_declared_external_and_secured_on_both_path_sets(api):
    schema = api[0].openapi()
    for path in READINGS_PATHS:
        operation = schema["paths"][path]["get"]
        assert operation["security"]
        assert operation[EXPOSURE_KEY] == EXPOSURE_EXTERNAL


def test_no_route_outside_the_readings_path_accepts_the_display_token(api):
    # Derived from the app rather than the REFUSED list, so a route added later
    # without a test entry cannot quietly inherit the display credential.
    app, _db, _scheduler = api
    for path, methods in app.openapi()["paths"].items():
        for method, operation in methods.items():
            if not isinstance(operation, dict) or not operation.get("security"):
                continue
            if method.upper() == "GET" and path in READINGS_PATHS:
                continue
            method_key = method.upper() if method.upper() != "HEAD" else "GET"
            # Path parameters exist only to identify a job the token may not
            # ask about; any well-formed value is refused the same way.
            concrete = re.sub(r"\{[^}]+\}", "none", path)
            response = request(app, concrete, method=method_key)
            assert response.status_code == 401, f"{method_key} {concrete} -> {response.status_code}"
