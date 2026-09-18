from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest

from usage_dashboard.server.codex_app_server import CodexLoginRequired
from usage_dashboard.server.fetch_claude import fetch_claude_usage, refresh_claude_token
from usage_dashboard.server.fetch_codex import fetch_codex_usage, parse_rate_limits
from usage_dashboard.server.fetch_ollama import _parse_relative_reset, fetch_ollama_usage
from usage_dashboard.server.fetch_opencode import (
    _parse_duration_granularity,
    fetch_opencode_usage,
)
from usage_dashboard.server.fetch_types import (
    FetchAuthError,
    FetchError,
    FetchRateLimitError,
)
from usage_dashboard.server.fetch_umans import fetch_umans_wallet
from usage_dashboard.server.fetch_zai import _format_tokens, fetch_zai_usage
from usage_dashboard.shared.models import (
    ALERT_CRIT,
    ALERT_NONE,
    ALERT_WARN,
    Provider,
    ReadingStatus,
)


def _claude_response_data():
    # Mirrors the live /api/oauth/usage shape observed 2026-07-10: the window
    # blocks carry `utilization` + `resets_at` (the old `utilization_percent` +
    # `reset_time` names were dropped). The aggregate five_hour/seven_day blocks
    # drive the Session/Weekly bars; per-model windows (e.g. Fable) appear only
    # as `weekly_scoped` entries in `limits[]` with a `scope.model.display_name`.
    return {
        "five_hour": {
            "utilization": 65.0,
            "resets_at": "2026-01-15T10:00:00Z",
            "limit_dollars": None,
        },
        "seven_day": {
            "utilization": 45.0,
            "resets_at": "2026-01-19T00:00:00Z",
            "limit_dollars": None,
        },
        # seven_day_opus/sonnet exist but are null on plans without those caps.
        "seven_day_opus": None,
        "limits": [
            {
                "kind": "session",
                "group": "session",
                "percent": 65,
                "severity": "normal",
                "resets_at": "2026-01-15T10:00:00+00:00",
                "scope": None,
                "is_active": False,
            },
            {
                "kind": "weekly_all",
                "group": "weekly",
                "percent": 45,
                "severity": "normal",
                "resets_at": "2026-01-19T00:00:00+00:00",
                "scope": None,
                "is_active": True,
            },
            {
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": 13,
                "severity": "normal",
                "resets_at": "2026-01-19T00:00:00+00:00",
                "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
                "is_active": False,
            },
        ],
    }


def _runtime_string_constants(path):
    """String literals a module evaluates at runtime, excluding docstrings."""
    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _codex_rate_limits(primary=None, secondary=None, by_limit_id=True):
    """The LIVE `account/rateLimits/read` result (codex-cli 0.155.1).

    Captured from a real ChatGPT Pro account, including the second
    `base_model_inference` bucket that must never be folded into the tile.
    NOTE the units: `windowDurationMins` is MINUTES (weekly = 10080).
    """
    bucket = {
        "limitId": "codex",
        "limitName": None,
        "primary": primary,
        "secondary": secondary,
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
        "planType": "pro",
    }
    payload = {"rateLimits": bucket, "accountId": "acct-1"}
    if by_limit_id:
        payload["rateLimitsByLimitId"] = {
            "codex": bucket,
            "base_model_inference": {
                "limitId": "base_model_inference",
                "limitName": "gpt-reserve",
                "primary": {
                    "usedPercent": 7,
                    "windowDurationMins": 10080,
                    "resetsAt": 1790377124,
                },
                "secondary": None,
            },
        }
    return payload


def _window(percent, minutes, resets_at=1778000000):
    return {
        "usedPercent": percent,
        "windowDurationMins": minutes,
        "resetsAt": resets_at,
    }


def _zai_response_data():
    # Mirrors the live response shape observed 2026-06-12: enveloped payload,
    # epoch-millisecond reset times, session = TOKENS_LIMIT unit 3.
    return {
        "code": 200,
        "msg": "Operation successful",
        "data": {
            "limits": [
                {
                    "type": "TOKENS_LIMIT",
                    "unit": 3,
                    "number": 5,
                    "percentage": 55,
                    "nextResetTime": 1781310923670,
                },
                {
                    "type": "TOKENS_LIMIT",
                    "unit": 6,
                    "number": 1,
                    "percentage": 35,
                    "nextResetTime": 1781663372979,
                },
                {
                    "type": "TIME_LIMIT",
                    "unit": 5,
                    "usage": 1000,
                    "currentValue": 37,
                    "percentage": 3,
                    "nextResetTime": 1783477772998,
                    "usageDetails": [
                        {"modelCode": "search-prime", "usage": 24},
                        {"modelCode": "web-reader", "usage": 10},
                        {"modelCode": "zread", "usage": 3},
                    ],
                },
            ]
        },
    }


def _ollama_html(session_pct="72.5", weekly_pct="42.0"):
    return f"""
    <html><body>
    <span>Cloud Usage</span><span>Pro</span>
    <div><h3>Session usage</h3>
      <div><div style="width: {session_pct}%"></div></div>
      <span>{session_pct}% used</span>
      <span>Resets in <time data-time="2026-06-13T02:00:00Z">2h</time></span>
    </div>
    <div><h3>Weekly usage</h3>
      <div><div style="width: {weekly_pct}%">
        <button type="button" style="width: 60%; background: #76b900"
          data-usage-segment data-model="nemotron-3-ultra" data-requests="588"
          aria-label="nemotron-3-ultra: 588 requests"></button>
        <button type="button" style="width: 30%; background: #ef4461"
          data-usage-segment data-model="minimax-m3" data-requests="1841"
          aria-label="minimax-m3: 1841 requests"></button>
      </div></div>
      <span>{weekly_pct}% used</span>
      <span>Resets in <time data-time="2026-06-17T00:00:00Z">4d</time></span>
    </div>
    </body></html>
    """


class TestFetchClaude:
    @patch("usage_dashboard.server.fetch_claude.httpx.Client")
    def test_fetch_claude_usage_returns_reading(self, mock_client_cls):
        mock_response = MagicMock()
        mock_response.json.return_value = _claude_response_data()
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        reading = fetch_claude_usage("test-token")
        assert reading.provider is Provider.CLAUDE
        assert reading.status is ReadingStatus.CURRENT
        assert reading.session_percent == 65.0
        assert reading.weekly_percent == 45.0
        assert reading.stale is False

    @patch("usage_dashboard.server.fetch_claude.httpx.Client")
    def test_fetch_claude_usage_accepts_claude_work_provider(self, mock_client_cls):
        mock_response = MagicMock()
        mock_response.json.return_value = _claude_response_data()
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        reading = fetch_claude_usage("test-token", Provider.CLAUDE_WORK)
        assert reading.provider is Provider.CLAUDE_WORK

    @patch("usage_dashboard.server.fetch_claude.httpx.Client")
    def test_fetch_claude_extracts_fable_scoped_limit(self, mock_client_cls):
        mock_response = MagicMock()
        mock_response.json.return_value = _claude_response_data()
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        reading = fetch_claude_usage("test-token")
        # Only the weekly_scoped (per-model) entry is surfaced; the unscoped
        # session/weekly_all entries duplicate the aggregate windows.
        assert reading.scoped_limits is not None
        assert len(reading.scoped_limits) == 1
        fable = reading.scoped_limits[0]
        assert fable.name == "Fable"
        assert fable.percent == 13.0
        assert fable.is_active is False
        assert fable.resets_at is not None

    @patch("usage_dashboard.server.fetch_claude.httpx.Client")
    def test_fetch_claude_no_limits_key_yields_no_scoped_limits(self, mock_client_cls):
        mock_response = MagicMock()
        data = _claude_response_data()
        del data["limits"]
        mock_response.json.return_value = data
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        reading = fetch_claude_usage("test-token")
        assert reading.scoped_limits is None

    @patch("usage_dashboard.server.fetch_claude.httpx.Client")
    def test_fetch_claude_raises_fetch_error_on_http_failure(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.get.side_effect = httpx.HTTPError("fail")
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        try:
            fetch_claude_usage("test-token")
            assert False, "Should have raised FetchError"
        except FetchError:
            pass

    @patch("usage_dashboard.server.fetch_claude.httpx.Client")
    def test_fetch_claude_raises_fetch_error_on_malformed_response(self, mock_client_cls):
        mock_response = MagicMock()
        mock_response.json.return_value = {"unexpected": "data"}
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        try:
            fetch_claude_usage("test-token")
            assert False, "Should have raised FetchError"
        except FetchError:
            pass

    @patch("usage_dashboard.server.fetch_claude.httpx.Client")
    def test_fetch_claude_idle_window_yields_none_not_stale(self, mock_client_cls):
        # When there's been no Claude activity in the trailing five hours, the
        # /oauth/usage endpoint returns the window's utilization/resets_at as
        # null. float(None) used to raise TypeError -> the tile went stale until
        # activity resumed. The idle window must parse to None and stay CURRENT.
        data = _claude_response_data()
        data["five_hour"] = {
            "utilization": None,
            "resets_at": None,
            "limit_dollars": None,
        }
        mock_response = MagicMock()
        mock_response.json.return_value = data
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        reading = fetch_claude_usage("test-token")
        assert reading.status is ReadingStatus.CURRENT
        assert reading.stale is False
        assert reading.session_percent is None
        assert reading.session_resets_at is None
        # The still-populated weekly window is unaffected.
        assert reading.weekly_percent == 45.0

    @patch("usage_dashboard.server.fetch_claude.httpx.Client")
    def test_fetch_claude_null_block_yields_none(self, mock_client_cls):
        # The whole window block may also come back as null.
        data = _claude_response_data()
        data["five_hour"] = None
        mock_response = MagicMock()
        mock_response.json.return_value = data
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        reading = fetch_claude_usage("test-token")
        assert reading.status is ReadingStatus.CURRENT
        assert reading.session_percent is None
        assert reading.session_resets_at is None

    @patch("usage_dashboard.server.fetch_claude.httpx.Client")
    def test_fetch_claude_converts_non_utc_offset(self, mock_client_cls):
        # A non-UTC offset must be CONVERTED to UTC, not stamped as UTC. The
        # aggregate path used to .replace(tzinfo=utc) the parsed wall-clock,
        # which would misplace such a value by the offset.
        data = _claude_response_data()
        data["five_hour"]["resets_at"] = "2026-01-15T15:00:00+05:00"
        mock_response = MagicMock()
        mock_response.json.return_value = data
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        reading = fetch_claude_usage("test-token")
        # 15:00+05:00 == 10:00 UTC.
        assert reading.session_resets_at == datetime(2026, 1, 15, 10, 0, 0)

    @patch("usage_dashboard.server.fetch_claude.httpx.Client")
    def test_fetch_claude_sends_authorization_header(self, mock_client_cls):
        mock_response = MagicMock()
        mock_response.json.return_value = _claude_response_data()
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        fetch_claude_usage("my-secret-token")
        call_args = mock_client.get.call_args
        headers = (
            call_args[1].get("headers", {})
            if call_args[1]
            else call_args[0][1] if len(call_args[0]) > 1 else {}
        )
        assert "Bearer my-secret-token" in str(headers) or any(
            "Bearer my-secret-token" in str(v) for v in headers.values()
        )


class TestParseCodexRateLimits:
    """Parsing only — the transport is codex_app_server's problem now."""

    SESSION_MINS = 300  # 5 h
    WEEKLY_MINS = 10080  # 7 d

    def test_standard_primary_and_secondary(self):
        reading = parse_rate_limits(
            _codex_rate_limits(
                primary=_window(42, self.SESSION_MINS, 1778000000),
                secondary=_window(71.5, self.WEEKLY_MINS, 1778600000),
            )
        )
        assert reading.provider is Provider.CODEX
        assert reading.status is ReadingStatus.CURRENT
        assert reading.session_percent == 42.0
        assert reading.weekly_percent == 71.5
        assert reading.session_resets_at == datetime(2026, 5, 5, 16, 53, 20)
        assert reading.stale is False

    def test_weekly_only_window_in_the_primary_slot(self):
        # The LIVE shape for a Pro account: the weekly window arrives in
        # `primary` with `secondary: null`. Classifying by slot would report a
        # 93% weekly burn as a session burn.
        reading = parse_rate_limits(
            _codex_rate_limits(primary=_window(93, self.WEEKLY_MINS), secondary=None)
        )
        assert reading.session_percent is None
        assert reading.weekly_percent == 93.0
        assert reading.weekly_resets_at == datetime(2026, 5, 5, 16, 53, 20)

    def test_weekly_minutes_are_not_mistaken_for_seconds(self):
        # Regression guard for the unit trap: the pre-migration threshold was
        # 100_000 SECONDS. A weekly window is 10_080 MINUTES, which is less
        # than 100_000, so reusing that constant silently classifies every
        # weekly window as a session window.
        reading = parse_rate_limits(
            _codex_rate_limits(primary=_window(93, self.WEEKLY_MINS), secondary=None)
        )
        assert reading.weekly_percent == 93.0, (
            "a 10080-minute window must classify as weekly"
        )

    def test_session_only_window_in_the_secondary_slot(self):
        reading = parse_rate_limits(
            _codex_rate_limits(primary=None, secondary=_window(12, self.SESSION_MINS))
        )
        assert reading.session_percent == 12.0
        assert reading.weekly_percent is None

    def test_session_only_window_in_the_primary_slot(self):
        reading = parse_rate_limits(
            _codex_rate_limits(primary=_window(12, self.SESSION_MINS), secondary=None)
        )
        assert reading.session_percent == 12.0
        assert reading.weekly_percent is None

    def test_by_limit_id_is_preferred_over_the_compatibility_bucket(self):
        payload = _codex_rate_limits(
            primary=_window(42, self.SESSION_MINS), secondary=None
        )
        # Make the two disagree so the assertion can only pass one way.
        payload["rateLimits"] = {
            "limitId": "codex",
            "primary": _window(99, self.SESSION_MINS),
            "secondary": None,
        }
        assert parse_rate_limits(payload).session_percent == 42.0

    def test_compatibility_response_with_only_rate_limits(self):
        payload = _codex_rate_limits(
            primary=_window(55, self.SESSION_MINS), secondary=None, by_limit_id=False
        )
        assert "rateLimitsByLimitId" not in payload
        assert parse_rate_limits(payload).session_percent == 55.0

    def test_other_buckets_are_ignored(self):
        # base_model_inference sits at 7% in the fixture; folding it in would
        # mix two unrelated limits into one tile.
        reading = parse_rate_limits(
            _codex_rate_limits(primary=_window(42, self.SESSION_MINS), secondary=None)
        )
        assert reading.session_percent == 42.0
        assert reading.weekly_percent is None

    def test_null_and_missing_windows(self):
        reading = parse_rate_limits(_codex_rate_limits(primary=None, secondary=None))
        assert reading.session_percent is None
        assert reading.weekly_percent is None
        assert reading.session_resets_at is None
        assert reading.weekly_resets_at is None

    def test_missing_percentages_and_reset_times(self):
        reading = parse_rate_limits(
            _codex_rate_limits(
                primary={"windowDurationMins": 300}, secondary={"usedPercent": None}
            )
        )
        assert reading.session_percent is None
        assert reading.weekly_percent is None

    def test_malformed_response_raises_a_contained_error(self):
        with pytest.raises(FetchError):
            parse_rate_limits({"nothing": "useful"})


class TestFetchCodexUsage:
    """The thin layer between the App Server client and the parser."""

    def test_fetches_through_the_client(self):
        client = MagicMock()
        client.read_account_and_rate_limits.return_value = (
            {"account": {"type": "chatgpt"}},
            _codex_rate_limits(primary=_window(42, 300), secondary=None),
        )
        reading = fetch_codex_usage(client)
        assert reading.provider is Provider.CODEX
        assert reading.session_percent == 42.0

    def test_login_required_propagates(self):
        client = MagicMock()
        client.read_account_and_rate_limits.side_effect = CodexLoginRequired(
            "Codex login required"
        )
        with pytest.raises(CodexLoginRequired):
            fetch_codex_usage(client)

    def test_no_openai_oauth_surface_remains(self):
        """Acceptance criterion: the custom integration is gone, not just unused.

        Checks *runtime* string constants across the package — docstrings and
        comments are excluded, because this module's own prose necessarily
        names what was removed.
        """
        forbidden = (
            "app_EMoamEEZ73f0CkXaXp7hrann",  # Codex CLI OAuth client id
            "auth.openai.com",  # OpenAI token endpoint
            "codex_cli_rs",  # originator spoofing
            "backend-api/wham/usage",  # the internal usage endpoint
        )
        offenders = []
        for path in sorted(pathlib.Path("src/usage_dashboard").rglob("*.py")):
            for literal in _runtime_string_constants(path):
                for marker in forbidden:
                    if marker in literal:
                        offenders.append(f"{path}: {marker}")
        assert not offenders, offenders


class TestFetchZai:
    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_fetch_zai_usage_returns_reading(self, mock_client_cls):
        mock_response = MagicMock()
        mock_response.json.return_value = _zai_response_data()
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        reading = fetch_zai_usage("test-key")
        assert reading.provider is Provider.ZAI
        assert reading.status is ReadingStatus.CURRENT
        assert reading.session_percent == 55.0
        assert reading.weekly_percent == 35.0
        assert reading.stale is False
        assert reading.models is not None
        assert len(reading.models) == 3
        assert reading.models[0].name == "search-prime"
        assert reading.models[0].requests == 24
        assert reading.models[0].share_percent == pytest.approx(64.864, abs=0.1)

    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_fetch_zai_raises_fetch_error_on_http_failure(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.get.side_effect = httpx.HTTPError("fail")
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        try:
            fetch_zai_usage("test-key")
            assert False, "Should have raised FetchError"
        except FetchError:
            pass

    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_fetch_zai_raises_fetch_error_on_malformed_response(self, mock_client_cls):
        mock_response = MagicMock()
        mock_response.json.return_value = {"wrong": "shape"}
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        try:
            fetch_zai_usage("test-key")
            assert False, "Should have raised FetchError"
        except FetchError:
            pass

    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_fetch_zai_missing_session_limit_raises(self, mock_client_cls):
        data = _zai_response_data()
        data["data"]["limits"] = [
            entry for entry in data["data"]["limits"] if entry.get("unit") != 3
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = data
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        try:
            fetch_zai_usage("test-key")
            assert False, "Should have raised FetchError"
        except FetchError:
            pass

    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_fetch_zai_missing_weekly_limit_raises(self, mock_client_cls):
        data = _zai_response_data()
        data["data"]["limits"] = [
            entry for entry in data["data"]["limits"] if entry.get("unit") != 6
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = data
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        try:
            fetch_zai_usage("test-key")
            assert False, "Should have raised FetchError"
        except FetchError:
            pass

    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_fetch_zai_no_usage_details_yields_none_models(self, mock_client_cls):
        data = _zai_response_data()
        for entry in data["data"]["limits"]:
            if entry.get("unit") == 5:
                del entry["usageDetails"]
        mock_response = MagicMock()
        mock_response.json.return_value = data
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        reading = fetch_zai_usage("test-key")
        assert reading.models is None

    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_fetch_zai_no_tools_entry_yields_none_models(self, mock_client_cls):
        data = _zai_response_data()
        data["data"]["limits"] = [
            entry for entry in data["data"]["limits"] if entry.get("unit") != 5
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = data
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        reading = fetch_zai_usage("test-key")
        assert reading.models is None


def _zai_model_usage_data():
    # Daily model-usage buckets (tokensUsage / modelCallCount) summing to
    # req 2273 / tok 284.0M — the shape of the live /model-usage response
    # observed 2026-08-03.
    return {
        "code": 200,
        "data": {
            "tokensUsage": [100_000_000, 50_000_000, 134_000_000],
            "modelCallCount": [1000, 1000, 273],
        },
    }


def _mock_zai_client(mock_client_cls, data, model_data=None, model_error=False):
    """Mock the two z.ai GETs: /quota/limit (*data*) and /model-usage.

    *model_data* defaults to :func:`_zai_model_usage_data`; *model_error*
    makes only the model-usage call fail (the omit-token-line path). Mirror of
    the retired umans history mock.
    """
    usage_response = MagicMock()
    usage_response.json.return_value = data
    usage_response.raise_for_status = MagicMock()
    model_response = MagicMock()
    model_response.json.return_value = (
        model_data if model_data is not None else _zai_model_usage_data()
    )
    model_response.raise_for_status = MagicMock()

    def _get(url, **kwargs):
        if url.endswith("/model-usage"):
            if model_error:
                raise httpx.ConnectError("model-usage down")
            return model_response
        return usage_response

    mock_client = MagicMock()
    mock_client.get.side_effect = _get
    mock_client_cls.return_value.__enter__.return_value = mock_client
    return mock_client


class TestFetchZaiWeeklyTokens:
    # Weekly-window token tracking (following the retired umans history
    # pattern): a second GET to /model-usage sums the current weekly quota
    # window and the reading's detail + alert carry the result.

    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_weekly_token_line_from_model_usage(self, mock_client_cls):
        _mock_zai_client(mock_client_cls, _zai_response_data())

        reading = fetch_zai_usage("test-key")

        assert reading.status == ReadingStatus.CURRENT
        assert reading.detail == "week req 2273  tok 284.0M"
        # 284.0M >= the default 240M warn tier.
        assert reading.alert == ALERT_WARN

    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_model_usage_request_covers_weekly_window(self, mock_client_cls):
        # nextResetTime is always in the FUTURE in production, so the fixture
        # must be future-dated too: the first cut of this test used a stale
        # 2026-06 timestamp, which made an inverted (start > end) range look
        # like a valid one and hid the bug that the window started at the
        # reset instant rather than one window before it.
        reset = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=2)
        data = _zai_response_data()
        weekly = [
            e for e in data["data"]["limits"]
            if e["type"] == "TOKENS_LIMIT" and e["unit"] == 6
        ][0]
        weekly["nextResetTime"] = int(reset.timestamp() * 1000)
        mock_client = _mock_zai_client(mock_client_cls, data)

        fetch_zai_usage("test-key")

        model_calls = [
            c for c in mock_client.get.call_args_list if c.args[0].endswith("/model-usage")
        ]
        assert len(model_calls) == 1
        params = model_calls[0].kwargs["params"]
        assert params["granularity"] == "day"
        # The window that is currently accruing began one week before the next
        # reset, expressed on the endpoint's UTC+8 clock.
        expected_start = reset - timedelta(days=7) + timedelta(hours=8)
        assert params["startTime"] == expected_start.strftime("%Y-%m-%d %H:%M:%S")
        assert params["startTime"] < params["endTime"], "window must not be inverted"

    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_model_usage_window_is_never_inverted(self, mock_client_cls):
        # Guard the actual production failure: a future reset time used as the
        # window start returns HTTP 200 with tokensUsage=None, so the line just
        # never appeared. Assert we never issue such a request.
        reset = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=6)
        data = _zai_response_data()
        weekly = [
            e for e in data["data"]["limits"]
            if e["type"] == "TOKENS_LIMIT" and e["unit"] == 6
        ][0]
        weekly["nextResetTime"] = int(reset.timestamp() * 1000)
        mock_client = _mock_zai_client(mock_client_cls, data)

        reading = fetch_zai_usage("test-key")

        params = [
            c for c in mock_client.get.call_args_list
            if c.args[0].endswith("/model-usage")
        ][0].kwargs["params"]
        assert params["startTime"] < params["endTime"]
        assert reading.detail is not None, "the weekly token line must be present"

    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_model_usage_prefers_the_api_totals(self, mock_client_cls):
        # granularity=day is ignored by the endpoint (it answers hourly), so the
        # API's own totalUsage is the trustworthy figure, not the bucket arrays.
        _mock_zai_client(
            mock_client_cls,
            _zai_response_data(),
            model_data={
                "granularity": "hourly",
                "tokensUsage": [1, 1],
                "modelCallCount": [1, 1],
                "totalUsage": {
                    "totalTokensUsage": 283_816_766,
                    "totalModelCallCount": 2273,
                },
            },
        )

        reading = fetch_zai_usage("test-key")

        assert reading.detail == "week req 2273  tok 283.8M"

    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_model_usage_failure_omits_token_line(self, mock_client_cls):
        # History is telemetry: its failure must not fail the reading — the
        # token line is omitted and the percentage bars remain.
        _mock_zai_client(mock_client_cls, _zai_response_data(), model_error=True)

        reading = fetch_zai_usage("test-key")

        assert reading.status == ReadingStatus.CURRENT
        assert reading.detail is None
        assert reading.alert == ALERT_NONE

    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_model_usage_without_lists_falls_back(self, mock_client_cls):
        _mock_zai_client(
            mock_client_cls, _zai_response_data(), model_data={"unexpected": True}
        )

        reading = fetch_zai_usage("test-key")

        assert reading.detail is None
        assert reading.alert == ALERT_NONE

    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_weekly_alert_none_below_warn_threshold(self, mock_client_cls):
        _mock_zai_client(mock_client_cls, _zai_response_data())

        reading = fetch_zai_usage("test-key", tokens_warn=999_000_000)

        assert reading.alert == ALERT_NONE

    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_weekly_alert_crit_wins_over_warn(self, mock_client_cls):
        _mock_zai_client(mock_client_cls, _zai_response_data())

        reading = fetch_zai_usage(
            "test-key", tokens_warn=100_000_000, tokens_crit=284_000_000
        )

        assert reading.alert == ALERT_CRIT

    def test_format_tokens_scales(self):
        assert _format_tokens(284_000_000) == "284.0M"
        assert _format_tokens(1_500_000_000) == "1.5B"
        assert _format_tokens(42_000) == "42.0k"
        assert _format_tokens(999) == "999"


class TestFetchOllama:
    @staticmethod
    def _mock_get(mock_client_cls, text="", status_code=200):
        response = MagicMock()
        response.text = text
        response.status_code = status_code
        response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client
        return mock_client

    @patch("usage_dashboard.server.fetch_ollama.httpx.Client")
    def test_fetch_ollama_usage_returns_reading(self, mock_client_cls):
        self._mock_get(mock_client_cls, text=_ollama_html())

        reading = fetch_ollama_usage("session=abc123")
        assert reading.provider is Provider.OLLAMA
        assert reading.status is ReadingStatus.CURRENT
        assert reading.session_percent == 72.5
        assert reading.weekly_percent == 42.0
        assert reading.session_resets_at == datetime(2026, 6, 13, 2, 0, 0)
        assert reading.weekly_resets_at == datetime(2026, 6, 17, 0, 0, 0)
        assert reading.stale is False
        assert reading.models is not None
        assert len(reading.models) == 2
        assert reading.models[0].name == "nemotron-3-ultra"
        assert reading.models[0].share_percent == 60.0
        assert reading.models[0].requests == 588
        assert reading.models[1].name == "minimax-m3"
        assert reading.models[1].share_percent == 30.0

    @patch("usage_dashboard.server.fetch_ollama.httpx.Client")
    def test_fetch_ollama_weekly_reset_far_after_label(self, mock_client_cls):
        # On the live page the weekly bar renders a long row of segment <button>s
        # between "Weekly usage" and its "Resets in …" text (~4.6k chars), and
        # it's the last section so nothing bounds the block. The reset must still
        # be parsed — the block is bounded by </section> or the end of the HTML.
        filler = '<button class="seg"></button>' * 200  # ~5.8k chars
        html = f"""
        <html><body><span>Cloud Usage</span><span>Pro</span>
        <div><h3>Session usage</h3><span>10.0% used</span>
          <span>Resets in <time data-time="2026-06-21T20:00:00Z">28m</time></span></div>
        <div><h3>Weekly usage</h3><span>33.1% used</span>
          {filler}
          <span>Resets in <time data-time="2026-06-22T00:00:00Z">4h</time></span></div>
        </body></html>
        """
        self._mock_get(mock_client_cls, text=html)

        reading = fetch_ollama_usage("session=abc123")
        assert reading.weekly_percent == 33.1
        assert reading.weekly_resets_at == datetime(2026, 6, 22, 0, 0, 0)

    @patch("usage_dashboard.server.fetch_ollama.httpx.Client")
    def test_fetch_ollama_weekly_block_bounded_by_section_tag(self, mock_client_cls):
        html = """
        <html><body><span>Cloud Usage</span><span>Pro</span>
        <section><div><h3>Session usage</h3><span>10.0% used</span>
          <span>Resets in <time data-time="2026-06-21T20:00:00Z">28m</time></span></div></section>
        <section><div><h3>Weekly usage</h3><span>33.1% used</span>
          <span>Resets in <time data-time="2026-06-22T00:00:00Z">4h</time></span></div>
          <div>noise after reset that should not be included</div></section>
        </body></html>
        """
        self._mock_get(mock_client_cls, text=html)
        reading = fetch_ollama_usage("session=abc123")
        assert reading.weekly_percent == 33.1
        assert reading.weekly_resets_at == datetime(2026, 6, 22, 0, 0, 0)

    @patch("usage_dashboard.server.fetch_ollama.httpx.Client")
    def test_fetch_ollama_parses_relative_reset_text(self, mock_client_cls):
        # The real settings page renders "Resets in N hours/days" text, not a
        # data-time attribute (WI-005). Reset times should be computed from it.
        html = """
        <html><body><span>Cloud Usage</span><span>Pro</span>
        <div><h3>Session usage</h3><span>10.0% used</span>
          <span>Resets in 5 hours</span></div>
        <div><h3>Weekly usage</h3><span>20.0% used</span>
          <span>Resets in 2 days</span></div>
        </body></html>
        """
        self._mock_get(mock_client_cls, text=html)

        before = datetime.now(timezone.utc).replace(tzinfo=None)
        reading = fetch_ollama_usage("session=abc123")
        after = datetime.now(timezone.utc).replace(tzinfo=None)

        assert reading.session_percent == 10.0
        assert reading.weekly_percent == 20.0
        sess = reading.session_resets_at
        wk = reading.weekly_resets_at
        assert sess is not None and wk is not None
        assert before + timedelta(hours=5) <= sess <= after + timedelta(hours=5)
        assert before + timedelta(days=2) <= wk <= after + timedelta(days=2)
        assert reading.session_reset_granularity == 3600
        assert reading.weekly_reset_granularity == 86400

    @patch("usage_dashboard.server.fetch_ollama.httpx.Client")
    def test_fetch_ollama_uses_smallest_displayed_reset_unit(self, mock_client_cls):
        html = """
        <html><body><span>Cloud Usage</span><span>Pro</span>
        <div><h3>Session usage</h3><span>10.0% used</span>
          <span>Resets in 1 day 3 hours</span></div>
        <div><h3>Weekly usage</h3><span>20.0% used</span>
          <span>Resets in 2 days 4 hours 30 minutes</span></div>
        </body></html>
        """
        self._mock_get(mock_client_cls, text=html)

        reading = fetch_ollama_usage("session=abc123")

        assert reading.session_reset_granularity == 3600
        assert reading.weekly_reset_granularity == 60

    @patch("usage_dashboard.server.fetch_ollama.httpx.Client")
    def test_fetch_ollama_sends_cookie_header(self, mock_client_cls):
        client = self._mock_get(mock_client_cls, text=_ollama_html())

        fetch_ollama_usage("session=abc123")
        headers = client.get.call_args[1]["headers"]
        assert headers["Cookie"] == "session=abc123"

    @patch("usage_dashboard.server.fetch_ollama.httpx.Client")
    def test_fetch_ollama_hourly_label_also_accepted(self, mock_client_cls):
        html = _ollama_html().replace("Session usage", "Hourly usage")
        self._mock_get(mock_client_cls, text=html)

        reading = fetch_ollama_usage("session=abc123")
        assert reading.session_percent == 72.5

    @patch("usage_dashboard.server.fetch_ollama.httpx.Client")
    def test_fetch_ollama_bar_width_fallback(self, mock_client_cls):
        html = _ollama_html().replace("% used", " percent")
        self._mock_get(mock_client_cls, text=html)

        reading = fetch_ollama_usage("session=abc123")
        assert reading.session_percent == 72.5

    @patch("usage_dashboard.server.fetch_ollama.httpx.Client")
    def test_fetch_ollama_raises_fetch_error_on_http_failure(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.get.side_effect = httpx.HTTPError("fail")
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        with pytest.raises(FetchError):
            fetch_ollama_usage("session=abc123")

    @patch("usage_dashboard.server.fetch_ollama.httpx.Client")
    def test_fetch_ollama_401_raises_auth_error(self, mock_client_cls):
        self._mock_get(mock_client_cls, status_code=401)

        with pytest.raises(FetchAuthError):
            fetch_ollama_usage("session=abc123")

    @patch("usage_dashboard.server.fetch_ollama.httpx.Client")
    def test_fetch_ollama_signed_out_page_raises_auth_error(self, mock_client_cls):
        html = '<html><body><h1>Sign in to Ollama</h1><form action="/signin"></form></body></html>'
        self._mock_get(mock_client_cls, text=html)

        with pytest.raises(FetchAuthError):
            fetch_ollama_usage("session=expired")

    @patch("usage_dashboard.server.fetch_ollama.httpx.Client")
    def test_fetch_ollama_missing_usage_raises_fetch_error(self, mock_client_cls):
        self._mock_get(mock_client_cls, text="<html><body>nothing here</body></html>")

        with pytest.raises(FetchError):
            fetch_ollama_usage("session=abc123")

    @patch("usage_dashboard.server.fetch_ollama.httpx.Client")
    def test_fetch_ollama_session_only_still_returns_reading(self, mock_client_cls):
        html = _ollama_html()
        html = html[: html.index("<h3>Weekly usage</h3>")] + "</body></html>"
        self._mock_get(mock_client_cls, text=html)

        reading = fetch_ollama_usage("session=abc123")
        assert reading.session_percent == 72.5
        assert reading.weekly_percent is None
        assert reading.models is None


class TestRefreshClaudeToken:
    @patch("usage_dashboard.server.fetch_claude.httpx.post")
    def test_refresh_returns_new_tokens(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        access, refresh = refresh_claude_token("old-refresh")
        assert access == "new-access"
        assert refresh == "new-refresh"

    @patch("usage_dashboard.server.fetch_claude.httpx.post")
    def test_refresh_uses_client_id_when_provided(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        refresh_claude_token("old-refresh", client_id="my-client-id")
        call_args = mock_post.call_args
        payload = call_args[1]["json"] if "json" in call_args[1] else call_args[0][1]
        assert payload["client_id"] == "my-client-id"

    @patch("usage_dashboard.server.fetch_claude.httpx.post")
    def test_refresh_reuses_old_refresh_when_not_returned(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "new-access",
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        access, refresh = refresh_claude_token("old-refresh")
        assert access == "new-access"
        assert refresh == "old-refresh"

    @patch("usage_dashboard.server.fetch_claude.httpx.post")
    def test_refresh_raises_fetch_error_on_http_failure(self, mock_post):
        mock_post.side_effect = httpx.HTTPError("fail")
        try:
            refresh_claude_token("old-refresh")
            assert False, "Should have raised FetchError"
        except FetchError:
            pass

    @patch("usage_dashboard.server.fetch_claude.httpx.post")
    def test_refresh_raises_fetch_error_on_malformed_response(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {"no_access_token": True}
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        try:
            refresh_claude_token("old-refresh")
            assert False, "Should have raised FetchError"
        except FetchError:
            pass


class TestFetchErrorClassification:
    @staticmethod
    def _mock_status_client(mock_client_cls, status_code, headers=None):
        mock_response = MagicMock()
        mock_response.status_code = status_code
        mock_response.headers = headers or {}
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=MagicMock(), response=mock_response
        )
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

    @patch("usage_dashboard.server.fetch_claude.httpx.Client")
    def test_claude_401_raises_auth_error(self, mock_client_cls):
        self._mock_status_client(mock_client_cls, 401)
        with pytest.raises(FetchAuthError):
            fetch_claude_usage("token")

    @patch("usage_dashboard.server.fetch_claude.httpx.Client")
    def test_claude_403_raises_plain_fetch_error_not_auth(self, mock_client_cls):
        # A scope/permission 403 is permanent; it must NOT be a FetchAuthError,
        # so the scheduler never tries to refresh (and rotate) on it.
        self._mock_status_client(mock_client_cls, 403)
        with pytest.raises(FetchError) as excinfo:
            fetch_claude_usage("token")
        assert not isinstance(excinfo.value, FetchAuthError)
        assert "scope" in str(excinfo.value)

    @patch("usage_dashboard.server.fetch_claude.httpx.Client")
    def test_claude_429_raises_rate_limit_error_with_retry_after(self, mock_client_cls):
        from usage_dashboard.server.fetch_types import FetchRateLimitError

        self._mock_status_client(mock_client_cls, 429, headers={"retry-after": "608"})
        with pytest.raises(FetchRateLimitError) as excinfo:
            fetch_claude_usage("token")
        assert excinfo.value.retry_after_seconds == 608.0
        assert not isinstance(excinfo.value, FetchAuthError)

    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_zai_401_raises_auth_error(self, mock_client_cls):
        self._mock_status_client(mock_client_cls, 401)
        with pytest.raises(FetchAuthError):
            fetch_zai_usage("key")


class TestZaiResetTimes:
    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_epoch_millis_parsed_to_utc(self, mock_client_cls):
        mock_response = MagicMock()
        mock_response.json.return_value = _zai_response_data()
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        reading = fetch_zai_usage("key")

        assert reading.session_resets_at == datetime(2026, 6, 13, 0, 35, 23, 670000)
        assert reading.weekly_resets_at == datetime(2026, 6, 17, 2, 29, 32, 979000)

    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_float_counters_do_not_fail_the_fetch(self, mock_client_cls):
        # JSON numbers with a fractional part parse to Python floats; the old
        # int(str(x)) idiom raised ValueError on "…670.0" and failed the whole
        # fetch. Whole-valued floats must parse identically to ints.
        data = _zai_response_data()
        limits = data["data"]["limits"]
        limits[0]["nextResetTime"] = 1781310923670.0  # session reset as float
        limits[1]["nextResetTime"] = 1781663372979.0  # weekly reset as float
        limits[2]["currentValue"] = 37.0
        for detail in limits[2]["usageDetails"]:
            detail["usage"] = float(detail["usage"])
        mock_response = MagicMock()
        mock_response.json.return_value = data
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        reading = fetch_zai_usage("key")
        assert reading.session_resets_at == datetime(2026, 6, 13, 0, 35, 23, 670000)
        assert reading.weekly_resets_at == datetime(2026, 6, 17, 2, 29, 32, 979000)
        assert reading.models is not None
        assert reading.models[0].requests == 24

    @pytest.mark.parametrize("bad_epoch", ["not-a-time", "", True, 1.5])
    @patch("usage_dashboard.server.fetch_zai.httpx.Client")
    def test_malformed_nonempty_epoch_does_not_become_1970(
        self, mock_client_cls, bad_epoch
    ):
        data = _zai_response_data()
        data["data"]["limits"][0]["nextResetTime"] = bad_epoch
        mock_response = MagicMock()
        mock_response.json.return_value = data
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        with pytest.raises(FetchError):
            fetch_zai_usage("key")


class TestParseRelativeReset:
    _NOW = datetime(2026, 6, 14, 12, 0, 0)

    def test_hours(self):
        got = _parse_relative_reset("Resets in 5 hours", self._NOW)
        assert got == (self._NOW + timedelta(hours=5), 3600)

    def test_days(self):
        got = _parse_relative_reset("resets in 2 days", self._NOW)
        assert got == (self._NOW + timedelta(days=2), 86400)

    def test_combined_day_and_hours(self):
        # Granularity is the SMALLEST unit named: "1 day 3 hours" carries
        # hour-resolution even though days also appear.
        got = _parse_relative_reset("Resets in 1 day 3 hours", self._NOW)
        assert got == (self._NOW + timedelta(days=1, hours=3), 3600)

    def test_minutes_abbreviated(self):
        got = _parse_relative_reset("resets in 45 min", self._NOW)
        assert got == (self._NOW + timedelta(minutes=45), 60)

    def test_weeks(self):
        got = _parse_relative_reset("resets in 1 week", self._NOW)
        assert got == (self._NOW + timedelta(weeks=1), 604800)

    def test_no_match_returns_none(self):
        assert _parse_relative_reset("no countdown here", self._NOW) is None

    def test_phrase_without_duration_returns_none(self):
        assert _parse_relative_reset("resets in a moment", self._NOW) is None


class TestOpenCodeMarkupResolution:
    def test_resolution_uses_smallest_displayed_unit(self):
        assert _parse_duration_granularity("4 hours 47 minutes") == 60
        assert _parse_duration_granularity("2 days 23 hours") == 3600
        assert _parse_duration_granularity("26 days") == 86400
        assert _parse_duration_granularity("soon") is None


# ---------------------------------------------------------------------------
# OpenCode Go
#
# Fixture shapes were copied from a live capture of
# https://opencode.ai/workspace/<wrk_...>/go on 2026-08-07, not invented.
# ---------------------------------------------------------------------------

# The billing object earlier on the page also carries a `monthlyUsage` key, set
# to null. Anything matching it instead of the real window silently loses the
# monthly bar, so every SSR fixture keeps it in front.
_OPENCODE_BILLING_NOISE = (
    "reloadAmount:20,reloadAmountMin:10,monthlyLimit:null,monthlyUsage:null,"
    "timeMonthlyUsageUpdated:null,reloadError:null,"
)


def _opencode_ssr_html(
    rolling='status:"ok",resetInSec:17223,usagePercent:0',
    weekly='status:"ok",resetInSec:256748,usagePercent:13',
    monthly='status:"ok",resetInSec:2306645,usagePercent:7',
):
    """The SolidJS hydration blob, in the live nesting."""
    windows = []
    if rolling is not None:
        windows.append(f"rollingUsage:$R[36]={{{rolling}}}")
    if weekly is not None:
        windows.append(f"weeklyUsage:$R[37]={{{weekly}}}")
    if monthly is not None:
        windows.append(f"monthlyUsage:$R[38]={{{monthly}}}")
    return (
        "<html><body><script>"
        f"$R[28]($R[14],$R[33]={{{_OPENCODE_BILLING_NOISE}}});"
        '$R[28]($R[18],$R[34]={mine:!0,useBalance:!1,region:$R[35]=["us","eu"],'
        + ",".join(windows)
        + "});</script></body></html>"
    )


def _opencode_markup_item(label, percent, reset_slot="reset-time", reset_text="4 hours 47 minutes"):
    inner = (
        f'<!--$-->Resets in<!--/--> <!--$-->{reset_text}<!--/-->'
        if reset_slot == "reset-time"
        else "<!--$-->Reset now<!--/-->"
    )
    return (
        f'<div data-slot="usage-item"><div data-slot="usage-header">'
        f'<span data-slot="usage-label">{label}</span>'
        f'<span data-slot="usage-value"><!--$-->{percent}<!--/-->%</span></div>'
        f'<div data-slot="progress"><div data-slot="progress-bar" '
        f'style="width:{percent}%"></div></div>'
        f'<span data-slot="{reset_slot}">{inner}</span></div>'
    )


def _opencode_markup_html(items=None):
    """The rendered fallback markup, with no hydration blob present."""
    if items is None:
        items = [
            _opencode_markup_item("Rolling Usage", "0", reset_text="4 hours 47 minutes"),
            _opencode_markup_item("Weekly Usage", "13", reset_text="2 days 23 hours"),
            _opencode_markup_item("Monthly Usage", "7", reset_text="26 days 16 hours"),
        ]
    return "<html><body>" + "".join(items) + "</body></html>"


class TestFetchOpenCode:
    @staticmethod
    def _mock_get(mock_client_cls, text="", status_code=200, url=None):
        response = MagicMock()
        response.text = text
        response.status_code = status_code
        # A real URL matters: the signed-out check calls .url.host.endswith(),
        # and a bare MagicMock would return a truthy MagicMock from it — every
        # fetch would then look like an auth failure.
        response.url = httpx.URL(
            url or "https://opencode.ai/workspace/wrk_TEST/go"
        )
        response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client
        return mock_client

    @patch("usage_dashboard.server.fetch_opencode.httpx.Client")
    def test_ssr_blob_maps_three_windows(self, mock_client_cls):
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        self._mock_get(mock_client_cls, text=_opencode_ssr_html())

        reading = fetch_opencode_usage("wrk_TEST", "cookie-value")

        assert reading.provider is Provider.OPENCODE
        assert reading.status is ReadingStatus.CURRENT
        assert reading.stale is False
        assert reading.session_percent == 0.0
        assert reading.weekly_percent == 13.0
        # Reset times are relative countdowns, so assert the offset from the
        # fetch instant rather than an absolute timestamp.
        assert reading.session_resets_at is not None
        assert timedelta(seconds=17223) <= (
            reading.session_resets_at - before
        ) <= timedelta(seconds=17223 + 60)
        assert reading.weekly_resets_at is not None
        assert timedelta(seconds=256748) <= (
            reading.weekly_resets_at - before
        ) <= timedelta(seconds=256748 + 60)
        # Monthly has no first-class field; it rides as a scoped limit.
        assert reading.scoped_limits is not None
        (monthly,) = reading.scoped_limits
        assert monthly.name == "Monthly"
        assert monthly.percent == 7.0
        assert monthly.resets_at is not None
        assert timedelta(seconds=2306645) <= (
            monthly.resets_at - before
        ) <= timedelta(seconds=2306645 + 60)

    @patch("usage_dashboard.server.fetch_opencode.httpx.Client")
    def test_ssr_ignores_the_null_monthly_usage_in_the_billing_object(self, mock_client_cls):
        # The page carries `monthlyUsage:null` before the real window. A pattern
        # that matched it would drop the monthly bar entirely.
        html = _opencode_ssr_html()
        assert "monthlyUsage:null" in html  # the hazard is actually present
        self._mock_get(mock_client_cls, text=html)

        reading = fetch_opencode_usage("wrk_TEST", "cookie-value")
        assert reading.scoped_limits is not None
        assert reading.scoped_limits[0].percent == 7.0

    @patch("usage_dashboard.server.fetch_opencode.httpx.Client")
    def test_ssr_field_order_does_not_matter(self, mock_client_cls):
        self._mock_get(
            mock_client_cls,
            text=_opencode_ssr_html(
                rolling='usagePercent:42,status:"ok",resetInSec:600',
                weekly='resetInSec:1200,usagePercent:8',
                monthly=None,
            ),
        )
        reading = fetch_opencode_usage("wrk_TEST", "cookie-value")
        assert reading.session_percent == 42.0
        assert reading.weekly_percent == 8.0
        assert reading.scoped_limits is None

    @patch("usage_dashboard.server.fetch_opencode.httpx.Client")
    def test_markup_fallback_when_no_hydration_blob(self, mock_client_cls):
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        self._mock_get(mock_client_cls, text=_opencode_markup_html())

        reading = fetch_opencode_usage("wrk_TEST", "cookie-value")
        assert reading.session_percent == 0.0
        assert reading.weekly_percent == 13.0
        assert reading.scoped_limits is not None
        assert reading.scoped_limits[0].percent == 7.0
        # The markup countdown is rounded to whole units: 4h47m, not 17223s.
        assert reading.session_resets_at is not None
        assert timedelta(seconds=17220) <= (
            reading.session_resets_at - before
        ) <= timedelta(seconds=17220 + 60)
        assert reading.session_reset_granularity == 60
        assert reading.weekly_reset_granularity == 3600
        assert reading.scoped_limits is not None
        assert reading.scoped_limits[0].reset_granularity == 3600

    @patch("usage_dashboard.server.fetch_opencode.httpx.Client")
    def test_ssr_wins_over_markup_when_both_present(self, mock_client_cls):
        # The live page carries both. The hydration values are exact, so a page
        # whose markup disagrees must still report the hydration numbers.
        html = _opencode_ssr_html() + _opencode_markup_html(
            items=[_opencode_markup_item("Rolling Usage", "99", reset_text="1 hour")]
        )
        self._mock_get(mock_client_cls, text=html)

        reading = fetch_opencode_usage("wrk_TEST", "cookie-value")
        assert reading.session_percent == 0.0
        assert reading.session_reset_granularity is None

    @patch("usage_dashboard.server.fetch_opencode.httpx.Client")
    def test_markup_reset_now_is_zero_not_missing(self, mock_client_cls):
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        self._mock_get(
            mock_client_cls,
            text=_opencode_markup_html(
                items=[
                    _opencode_markup_item("Rolling Usage", "5", reset_slot="reset-now"),
                ]
            ),
        )
        reading = fetch_opencode_usage("wrk_TEST", "cookie-value")
        assert reading.session_resets_at is not None
        assert reading.session_resets_at - before < timedelta(seconds=60)

    @patch("usage_dashboard.server.fetch_opencode.httpx.Client")
    def test_markup_unparseable_countdown_leaves_reset_unknown(self, mock_client_cls):
        self._mock_get(
            mock_client_cls,
            text=_opencode_markup_html(
                items=[
                    _opencode_markup_item(
                        "Weekly Usage", "50", reset_text="soon"
                    ),
                ]
            ),
        )
        reading = fetch_opencode_usage("wrk_TEST", "cookie-value")
        assert reading.weekly_percent == 50.0
        assert reading.weekly_resets_at is None

    @patch("usage_dashboard.server.fetch_opencode.httpx.Client")
    def test_sign_in_redirect_is_an_auth_error(self, mock_client_cls):
        # Signed out (or a wrong workspace id): the site 302s to its auth host
        # and serves the sign-in page with HTTP 200. Status code alone is not
        # a usable signal — the final URL is.
        self._mock_get(
            mock_client_cls,
            text="<html><body>Sign in</body></html>",
            status_code=200,
            url="https://auth.opencode.ai/authorize?client_id=app",
        )
        with pytest.raises(FetchAuthError) as excinfo:
            fetch_opencode_usage("wrk_TEST", "stale-cookie")
        # Both causes are indistinguishable, so both must be named.
        assert "workspace id" in str(excinfo.value)

    @patch("usage_dashboard.server.fetch_opencode.httpx.Client")
    def test_lookalike_auth_host_is_not_an_auth_error(self, mock_client_cls):
        # The redirect check must match the auth host exactly, not any host that
        # merely ends with the same string: "subauth.opencode.ai" endswith
        # "auth.opencode.ai", but it is not the sign-in host and a parseable
        # page served from it is a normal fetch.
        self._mock_get(
            mock_client_cls,
            text=_opencode_ssr_html(),
            status_code=200,
            url="https://subauth.opencode.ai/workspace/wrk_TEST/go",
        )
        reading = fetch_opencode_usage("wrk_TEST", "cookie-value")
        assert reading.provider is Provider.OPENCODE

    @patch("usage_dashboard.server.fetch_opencode.httpx.Client")
    def test_http_401_is_an_auth_error(self, mock_client_cls):
        self._mock_get(mock_client_cls, text="", status_code=401)
        with pytest.raises(FetchAuthError):
            fetch_opencode_usage("wrk_TEST", "stale-cookie")

    @patch("usage_dashboard.server.fetch_opencode.httpx.Client")
    def test_unparseable_page_is_a_fetch_error_not_an_auth_error(self, mock_client_cls):
        # A page served from opencode.ai that we simply can't parse is a parser
        # regression, not a credential problem — it must not park the provider
        # at the auth backoff cap with a misleading "re-login" detail.
        self._mock_get(mock_client_cls, text="<html><body>redesigned</body></html>")
        with pytest.raises(FetchError) as excinfo:
            fetch_opencode_usage("wrk_TEST", "cookie-value")
        assert not isinstance(excinfo.value, FetchAuthError)

    def test_missing_workspace_id_is_a_fetch_error(self):
        with pytest.raises(FetchError):
            fetch_opencode_usage("", "cookie-value")

    @patch("usage_dashboard.server.fetch_opencode.httpx.Client")
    def test_cookie_is_sent_as_the_auth_cookie(self, mock_client_cls):
        client = self._mock_get(mock_client_cls, text=_opencode_ssr_html())
        fetch_opencode_usage("wrk_TEST", "cookie-value")
        url, kwargs = client.get.call_args[0][0], client.get.call_args[1]
        assert url == "https://opencode.ai/workspace/wrk_TEST/go"
        assert kwargs["headers"]["Cookie"] == "auth=cookie-value"


def _umans_wallet_response_data():
    # Live response shape 2026-09-09 (amounts from the real endpoint; the key
    # identity is a placeholder, as with wrk_TEST/acct-123 elsewhere in this
    # file — this repo is public). The documented balance object carries only
    # balanceCents/funded/asOf — no promo split has ever been observed; promo
    # parsing is forward-looking.
    return {
        "wallet": {"owner": "user"},
        "key": {
            "id": "00000000-0000-0000-0000-000000000000",
            "prefix": "umans_TEST",
            "kind": "service_account",
            "name": "OpenCode",
        },
        "scope": "mine",
        "balance": {
            "balanceCents": 1631.5388090000001,
            "funded": True,
            "asOf": "2026-09-09T04:10:53.460Z",
        },
        "spend": {
            "last24hCents": 40.44652,
            "last7dCents": 1663.099491,
            "last30dCents": 3354.461191,
        },
        "usage30d": {
            "requests": 9917,
            "tokensIn": 27483369,
            "tokensOut": 7740536,
            "tokensCachedRead": 1213484271,
        },
        "breakdown": [
            {
                "model": "umans-deepseek-v4-flash-0731",
                "keyName": "OpenCode",
                "keyPrefix": "umans_FZMw",
                "requests": 8327,
                "spendCents": 3322.199708,
            },
        ],
        "asOf": "2026-09-09T04:10:53.460Z",
    }


class TestFetchUmansWallet:
    def _mock_get(self, mock_client_cls, data=None, status=200, headers=None,
                  content=None):
        mock_response = MagicMock()
        if status >= 400:
            request = httpx.Request("GET", "https://app.umans.ai/api/v1/wallet/summary")
            if content is not None:
                resp = httpx.Response(
                    status, request=request, content=content, headers=headers
                )
                mock_response.json.side_effect = ValueError("not JSON")
            else:
                resp = httpx.Response(status, request=request, json=data,
                                      headers=headers)
                mock_response.json.return_value = data
            mock_response.status_code = status
            mock_response.content = resp.content
            mock_response.headers = resp.headers
            mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "err", request=request, response=resp
            )
        else:
            mock_response.status_code = 200
            mock_response.content = b"{}"
            mock_response.json.return_value = data
            mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client
        return mock_client

    @patch("usage_dashboard.server.fetch_umans.httpx.Client")
    def test_parses_live_shape_to_a_quotaless_reading(self, mock_client_cls):
        self._mock_get(mock_client_cls, _umans_wallet_response_data())
        reading = fetch_umans_wallet("test-key")
        assert reading.provider is Provider.UMANS
        assert reading.status is ReadingStatus.CURRENT
        assert reading.session_percent is None
        assert reading.weekly_percent is None
        assert reading.stale is False
        assert reading.detail == "$16.32"

    @patch("usage_dashboard.server.fetch_umans.httpx.Client")
    def test_sends_bearer_key_to_the_dashboard_host(self, mock_client_cls):
        client = self._mock_get(mock_client_cls, _umans_wallet_response_data())
        fetch_umans_wallet("test-key")
        url, kwargs = client.get.call_args[0][0], client.get.call_args[1]
        assert url == "https://app.umans.ai/api/v1/wallet/summary"
        assert kwargs["headers"]["Authorization"] == "Bearer test-key"

    @patch("usage_dashboard.server.fetch_umans.httpx.Client")
    def test_promo_cents_appends_the_promo_tail(self, mock_client_cls):
        data = _umans_wallet_response_data()
        data["balance"]["promoCents"] = 714.0
        data["balance"]["balanceCents"] = 875.0
        self._mock_get(mock_client_cls, data)
        reading = fetch_umans_wallet("test-key")
        assert reading.detail == "$8.75, promo: $7.14"

    @patch("usage_dashboard.server.fetch_umans.httpx.Client")
    def test_zero_or_missing_promo_shows_no_tail(self, mock_client_cls):
        data = _umans_wallet_response_data()
        data["balance"]["promoCents"] = 0
        self._mock_get(mock_client_cls, data)
        reading = fetch_umans_wallet("test-key")
        assert reading.detail == "$16.32"

    @patch("usage_dashboard.server.fetch_umans.httpx.Client")
    def test_404_wallet_not_found_is_a_current_no_wallet_reading(self, mock_client_cls):
        # An archived-plan key has no wallet behind it: a documented steady
        # state (umans usage renders its plan view on it), not a failure to
        # back off and retry forever.
        self._mock_get(
            mock_client_cls, {"error": "no wallet", "code": "wallet_not_found"},
            status=404,
        )
        reading = fetch_umans_wallet("test-key")
        assert reading.status is ReadingStatus.CURRENT
        assert reading.detail == "no wallet"

    @patch("usage_dashboard.server.fetch_umans.httpx.Client")
    def test_401_raises_auth_error(self, mock_client_cls):
        self._mock_get(
            mock_client_cls, {"error": "Invalid or revoked API key",
                              "code": "invalid_key"},
            status=401,
        )
        with pytest.raises(FetchAuthError):
            fetch_umans_wallet("bad-key")

    @patch("usage_dashboard.server.fetch_umans.httpx.Client")
    def test_403_raises_auth_error(self, mock_client_cls):
        # e.g. a personal wallet key asking for scope=organization is rejected
        # with 403; we never send scope, but any 403 is a credential problem.
        self._mock_get(
            mock_client_cls, {"error": "forbidden", "code": "scope_not_allowed"},
            status=403,
        )
        with pytest.raises(FetchAuthError):
            fetch_umans_wallet("test-key")

    @patch("usage_dashboard.server.fetch_umans.httpx.Client")
    def test_404_without_the_coded_body_is_a_fetch_error(self, mock_client_cls):
        # A proxy/HTML 404 page is NOT the documented archived-plan state.
        self._mock_get(mock_client_cls, content=b"<html>not found</html>",
                       status=404)
        with pytest.raises(FetchError) as excinfo:
            fetch_umans_wallet("test-key")
        assert not isinstance(excinfo.value, FetchAuthError)

    @patch("usage_dashboard.server.fetch_umans.httpx.Client")
    def test_404_with_uncoded_json_body_is_a_fetch_error(self, mock_client_cls):
        self._mock_get(mock_client_cls, {"error": "nope"}, status=404)
        with pytest.raises(FetchError):
            fetch_umans_wallet("test-key")

    @patch("usage_dashboard.server.fetch_umans.httpx.Client")
    def test_429_raises_rate_limit_error_with_retry_after(self, mock_client_cls):
        self._mock_get(mock_client_cls, {}, status=429, headers={"Retry-After": "120"})
        with pytest.raises(FetchRateLimitError) as excinfo:
            fetch_umans_wallet("test-key")
        assert excinfo.value.retry_after_seconds == 120.0

    @patch("usage_dashboard.server.fetch_umans.httpx.Client")
    def test_429_httpdate_retry_after_parses(self, mock_client_cls):
        # RFC 7231 allows an HTTP-date; a future date must become seconds.
        retry_at = datetime.now(timezone.utc) + timedelta(hours=1)
        self._mock_get(
            mock_client_cls, {}, status=429,
            headers={"Retry-After": format_datetime(retry_at)},
        )
        with pytest.raises(FetchRateLimitError) as excinfo:
            fetch_umans_wallet("test-key")
        assert excinfo.value.retry_after_seconds is not None
        assert 3500 < excinfo.value.retry_after_seconds <= 3600

    @patch("usage_dashboard.server.fetch_umans.httpx.Client")
    def test_429_past_httpdate_yields_no_hint(self, mock_client_cls):
        self._mock_get(
            mock_client_cls, {}, status=429,
            headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"},
        )
        with pytest.raises(FetchRateLimitError) as excinfo:
            fetch_umans_wallet("test-key")
        assert excinfo.value.retry_after_seconds is None

    @patch("usage_dashboard.server.fetch_umans.httpx.Client")
    def test_429_without_retry_after_yields_no_hint(self, mock_client_cls):
        self._mock_get(mock_client_cls, {}, status=429)
        with pytest.raises(FetchRateLimitError) as excinfo:
            fetch_umans_wallet("test-key")
        assert excinfo.value.retry_after_seconds is None

    @patch("usage_dashboard.server.fetch_umans.httpx.Client")
    def test_missing_balance_raises_fetch_error(self, mock_client_cls):
        self._mock_get(mock_client_cls, {"wallet": {"owner": "user"}})
        with pytest.raises(FetchError) as excinfo:
            fetch_umans_wallet("test-key")
        assert not isinstance(excinfo.value, FetchAuthError)
