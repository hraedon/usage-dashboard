from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import httpx
import pytest

from usage_dashboard.cli import _capacity_url, capacity, main


def response(status_code: int, body: object = None) -> MagicMock:
    result = MagicMock()
    result.status_code = status_code
    result.json.return_value = body
    return result


def client_for(result: MagicMock) -> MagicMock:
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.get.return_value = result
    return client


def snapshot(*, assessment: str = "unknown") -> dict[str, object]:
    return {
        "schema_version": "agent-capacity-v1",
        "generated_at": "2026-09-05T12:00:00Z",
        "max_age_seconds": 900,
        "accounts": [{"account_id": "claude", "assessment": assessment}],
        "assessment_scope": "reported_limits_only",
        "job_fit": "not_assessed",
    }


def test_capacity_prints_200_snapshot_and_sends_optional_filters(monkeypatch, capsys) -> None:
    monkeypatch.setenv("USAGE_DASHBOARD_URL", "https://dashboard.example/api/v1/")
    monkeypatch.setenv("USAGE_DASHBOARD_AGENT_TOKEN", "agent-secret")
    body = snapshot(assessment="known_blocked")
    client = client_for(response(200, body))

    with patch("usage_dashboard.cli.httpx.Client", return_value=client) as client_factory:
        status = capacity(account="claude_work", max_age_seconds=3600)

    assert status == 0
    assert json.loads(capsys.readouterr().out) == body
    client_factory.assert_called_once_with(timeout=15.0)
    client.get.assert_called_once_with(
        "https://dashboard.example/api/v1/agent/capacity",
        headers={"Authorization": "Bearer agent-secret"},
        params={"account": "claude_work", "max_age_seconds": 3600},
    )


def test_capacity_accepts_unknown_observation_as_success(monkeypatch, capsys) -> None:
    monkeypatch.setenv("USAGE_DASHBOARD_URL", "http://localhost:8080")
    monkeypatch.setenv("USAGE_DASHBOARD_AGENT_TOKEN", "secret")
    body = snapshot()
    client = client_for(response(200, body))

    with patch("usage_dashboard.cli.httpx.Client", return_value=client):
        assert capacity() == 0

    assert json.loads(capsys.readouterr().out)["accounts"][0]["assessment"] == "unknown"


def test_capacity_rejects_missing_token_without_request(monkeypatch, capsys) -> None:
    monkeypatch.setenv("USAGE_DASHBOARD_URL", "https://dashboard.example")
    monkeypatch.delenv("USAGE_DASHBOARD_AGENT_TOKEN", raising=False)

    with patch("usage_dashboard.cli.httpx.Client") as client_factory:
        assert capacity() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "USAGE_DASHBOARD_AGENT_TOKEN" in captured.err
    client_factory.assert_not_called()


def test_capacity_hides_non_200_response_body(monkeypatch, capsys) -> None:
    monkeypatch.setenv("USAGE_DASHBOARD_URL", "https://dashboard.example")
    monkeypatch.setenv("USAGE_DASHBOARD_AGENT_TOKEN", "secret")
    client = client_for(response(401, {"detail": "secret response diagnostic"}))

    with patch("usage_dashboard.cli.httpx.Client", return_value=client):
        assert capacity() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "HTTP 401" in captured.err
    assert "secret response diagnostic" not in captured.err


def test_capacity_rejects_unsupported_schema(monkeypatch, capsys) -> None:
    monkeypatch.setenv("USAGE_DASHBOARD_URL", "https://dashboard.example")
    monkeypatch.setenv("USAGE_DASHBOARD_AGENT_TOKEN", "secret")
    client = client_for(response(200, {"schema_version": "agent-capacity-v2"}))

    with patch("usage_dashboard.cli.httpx.Client", return_value=client):
        assert capacity() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unsupported schema" in captured.err


def test_capacity_hides_transport_diagnostics(monkeypatch, capsys) -> None:
    monkeypatch.setenv("USAGE_DASHBOARD_URL", "https://dashboard.example")
    monkeypatch.setenv("USAGE_DASHBOARD_AGENT_TOKEN", "secret")
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.get.side_effect = httpx.ConnectError("response body and secret")

    with patch("usage_dashboard.cli.httpx.Client", return_value=client):
        assert capacity() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "capacity: capacity request failed"


@pytest.mark.parametrize("error", [
    httpx.InvalidURL("private endpoint diagnostic"),
    UnicodeEncodeError("ascii", "private-token-\u00e9", 14, 15, "ordinal out of range"),
])
def test_capacity_hides_request_construction_errors(monkeypatch, capsys, error):
    monkeypatch.setenv("USAGE_DASHBOARD_URL", "https://dashboard.example")
    monkeypatch.setenv("USAGE_DASHBOARD_AGENT_TOKEN", "secret")
    client = client_for(response(200, snapshot()))
    client.get.side_effect = error
    with patch("usage_dashboard.cli.httpx.Client", return_value=client):
        assert capacity() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "capacity: capacity request failed"


def test_capacity_url_accepts_origin_or_api_prefix() -> None:
    assert _capacity_url("https://example.test") == (
        "https://example.test/api/v1/agent/capacity"
    )
    assert _capacity_url("https://example.test/api/v1/") == (
        "https://example.test/api/v1/agent/capacity"
    )


def test_capacity_url_rejects_credentials_and_non_http() -> None:
    for value in ("https://user:password@example.test", "ftp://example.test"):
        try:
            _capacity_url(value)
        except Exception as exc:
            assert "credentials" in str(exc) or "HTTP(S)" in str(exc)
        else:
            raise AssertionError("invalid capacity URL was accepted")


def test_main_registers_capacity_command(monkeypatch, capsys) -> None:
    monkeypatch.setenv("USAGE_DASHBOARD_URL", "https://dashboard.example")
    monkeypatch.setenv("USAGE_DASHBOARD_AGENT_TOKEN", "secret")
    client = client_for(response(200, snapshot()))
    monkeypatch.setattr(
        sys,
        "argv",
        ["usage-dashboard", "capacity", "--account", "claude", "--max-age-seconds", "30"],
    )

    with patch("usage_dashboard.cli.httpx.Client", return_value=client):
        try:
            main()
        except SystemExit as exc:
            assert exc.code == 0
        else:
            raise AssertionError("capacity command did not exit with a status")

    assert json.loads(capsys.readouterr().out)["schema_version"] == "agent-capacity-v1"
