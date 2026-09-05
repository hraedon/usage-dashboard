from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import httpx
import pytest

from usage_dashboard.server.api import _same_api_key, create_app
from usage_dashboard.server.capacity import account_capacity
from usage_dashboard.server.db import Database
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus, ScopedLimit

NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


def reading(**overrides):
    base = Reading(
        Provider.CLAUDE, ReadingStatus.CURRENT, 30, NOW + timedelta(hours=1),
        50, NOW + timedelta(days=2), NOW - timedelta(seconds=60), False,
    )
    return replace(base, **overrides)


def assess(value, now=NOW):
    provider = value.provider if value is not None else Provider.CLAUDE
    return account_capacity(provider, value, now=now, max_age_seconds=900)


def test_fresh_headroom_is_not_a_job_prediction():
    result = assess(reading())
    assert result.assessment == "no_known_quota_block"
    assert result.age_seconds == 60
    assert result.windows[0].remaining_percent == 70
    assert result.observed_at.endswith("Z")


@pytest.mark.parametrize(
    "overrides,freshness,reason",
    [
        ({"fetched_at": NOW - timedelta(seconds=901)}, "stale", "stale_observation"),
        ({"stale": True}, "stale", "stale_observation"),
        ({"status": ReadingStatus.STALE}, "stale", "stale_observation"),
        ({"status": ReadingStatus.OFFLINE}, "offline", "provider_offline"),
        ({"fetched_at": NOW + timedelta(seconds=1)}, "invalid", "observation_in_future"),
    ],
)
def test_untrusted_reading_never_reports_available_or_blocked(overrides, freshness, reason):
    result = assess(reading(session_percent=100, **overrides))
    assert result.assessment == "unknown"
    assert result.freshness == freshness
    assert reason in result.reasons
    assert all(window.state == "unknown" for window in result.windows)


def test_missing_reading_has_no_fabricated_observation_timestamp():
    result = assess(None)
    assert result.assessment == "unknown"
    assert result.observed_at is None
    assert result.age_seconds is None
    assert result.reasons == ["no_reading"]


def test_exhausted_account_and_boxed_provider_block():
    assert assess(reading(weekly_percent=100)).assessment == "known_blocked"
    assert assess(reading(throttle="boxed")).reasons == ["provider_boxed"]


@pytest.mark.parametrize("active,expected", [(True, "known_blocked"), (False, "unknown")])
def test_scoped_exhaustion_is_not_silently_ignored(active, expected):
    result = assess(reading(scoped_limits=[
        ScopedLimit("model-A", 100, NOW + timedelta(days=1), active)
    ]))
    assert result.assessment == expected
    assert result.windows[-1].scope == "provider_scoped"
    if not active:
        assert result.reasons == ["scope_selection_required"]


def test_weekly_only_and_missing_limits_are_distinct():
    weekly = reading(session_percent=None, session_resets_at=None)
    assert assess(weekly).assessment == "no_known_quota_block"
    assert [w.name for w in assess(weekly).windows] == ["weekly"]
    empty = replace(weekly, weekly_percent=None, weekly_resets_at=None)
    assert assess(empty).reasons == ["no_reported_limits"]


def test_opencode_monthly_window_is_an_account_limit():
    result = assess(reading(
        provider=Provider.OPENCODE,
        scoped_limits=[ScopedLimit("Monthly", 100, NOW + timedelta(days=1))],
    ))
    assert result.assessment == "known_blocked"
    assert result.reasons == ["quota_exhausted"]
    assert result.windows[-1].scope == "account"
    assert result.windows[-1].is_active is True


@pytest.mark.parametrize("percent", [float("nan"), float("inf"), -1, 101, True])
def test_invalid_usage_is_unknown_and_json_safe(percent):
    result = assess(reading(session_percent=percent))
    assert result.assessment == "unknown"
    assert result.windows[0].used_percent is None
    assert result.windows[0].remaining_percent is None
    assert result.windows[0].reasons == ["invalid_usage"]


def test_reset_elapsed_does_not_replenish_or_keep_blocking():
    for percent in (10, 100):
        result = assess(reading(session_percent=percent, session_resets_at=NOW))
        assert result.assessment == "unknown"
        assert result.windows[0].reasons == ["reset_elapsed"]


def test_missing_usage_with_reset_is_not_headroom():
    assert assess(reading(session_percent=None)).assessment == "unknown"


def test_soft_or_unrecognized_throttle_does_not_claim_available():
    for throttle in ("rate_limited", "low", "future_throttle"):
        assert assess(reading(throttle=throttle)).reasons == ["provider_throttled"]


def test_naive_utc_and_offset_timestamps_compare_consistently():
    value = reading(fetched_at=(NOW - timedelta(seconds=60)).replace(tzinfo=None))
    assert assess(value, NOW.astimezone(timezone(timedelta(hours=5)))).age_seconds == 60


@pytest.fixture
def api(tmp_path):
    db = Database(str(tmp_path / "agents.db"))
    db.initialize()
    scheduler = MagicMock()
    app = create_app(
        "operator-test", db, configured_providers=[Provider.CLAUDE, Provider.CLAUDE_WORK],
        scheduler=scheduler, agent_api_key="agent-test",
    )
    return app, db, scheduler


def request(app, path="/api/v1/agent/capacity", key="agent-test", method="GET"):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Authorization": "Bearer " + key} if key else {}
            return await client.request(method, path, headers=headers)
    return asyncio.run(run())


def test_api_returns_separate_accounts_without_raw_detail_or_provider_poll(api):
    app, db, scheduler = api
    db.store_reading(reading(
        fetched_at=datetime.now(timezone.utc), session_resets_at=None,
        weekly_resets_at=None, detail="private provider diagnostic",
    ))
    response = request(app)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["schema_version"] == "agent-capacity-v1"
    assert body["job_fit"] == "not_assessed"
    assert body["assessment_scope"] == "reported_limits_only"
    assert [a["account_id"] for a in body["accounts"]] == ["claude", "claude_work"]
    assert body["accounts"][0]["assessment"] == "no_known_quota_block"
    assert body["accounts"][1]["freshness"] == "missing"
    assert "private provider diagnostic" not in response.text
    scheduler.fetch_now.assert_not_called()


@pytest.mark.parametrize("key", [None, "wrong"])
def test_agent_api_requires_auth(api, key):
    assert request(api[0], key=key).status_code == 401


@pytest.mark.parametrize("path", ["/readings", "/api/v1/history?provider=claude",
                                  "/api/v1/schedule", "/api/v1/refresh"])
def test_agent_token_cannot_access_other_endpoints(api, path):
    method = "POST" if path.endswith("refresh") else "GET"
    assert request(api[0], path, method=method).status_code == 401


def test_existing_operator_token_remains_compatible(api):
    assert request(api[0], key="operator-test").status_code == 200
    assert request(api[0], "/readings", key="operator-test").status_code == 200


def test_agent_token_optional_and_equal_keys_refused(api):
    app = create_app("operator-test", api[1])
    assert request(app, key="agent-test").status_code == 401
    assert request(app, key="operator-test").status_code == 200
    with pytest.raises(ValueError, match="must differ"):
        create_app("same", api[1], agent_api_key="same")


def test_key_comparison_accepts_unicode_text_without_coercing_identity():
    assert _same_api_key("test-\u00e9", "test-\u00e9")
    assert not _same_api_key("test-\u00e9", "test-e")


def test_filter_errors_and_no_legacy_alias(api):
    app = api[0]
    filtered = request(app, "/api/v1/agent/capacity?account=claude_work")
    assert [a["account_id"] for a in filtered.json()["accounts"]] == ["claude_work"]
    assert request(app, "/api/v1/agent/capacity?account=ollama").status_code == 404
    assert request(app, "/api/v1/agent/capacity?account=umans").status_code == 422
    for age in ("0", "86401", "nan"):
        assert request(app, f"/api/v1/agent/capacity?max_age_seconds={age}").status_code == 422
    assert request(app, "/agent/capacity").status_code == 404


def test_default_age_allows_normal_idle_polling_and_can_be_overridden(api):
    app, db, _scheduler = api
    db.store_reading(reading(
        fetched_at=datetime.now(timezone.utc) - timedelta(seconds=1800),
        session_resets_at=None, weekly_resets_at=None,
    ))
    default = request(app).json()
    assert default["max_age_seconds"] == 2100
    assert default["accounts"][0]["freshness"] == "fresh"
    strict = request(app, "/api/v1/agent/capacity?max_age_seconds=900").json()
    assert strict["accounts"][0]["freshness"] == "stale"


def test_openapi_defines_authenticated_typed_contract(api):
    schema = api[0].openapi()
    operation = schema["paths"]["/api/v1/agent/capacity"]["get"]
    assert operation["security"]
    assert operation["x-exposure"] == "external"
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert "AccountCapacity" in schema["components"]["schemas"]
