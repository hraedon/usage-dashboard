"""Exercise the CLI over real HTTP against an isolated cached-reading server."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import uvicorn

from usage_dashboard.server.api import create_app
from usage_dashboard.server.db import Database
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus


@pytest.fixture(scope="module")
def capacity_server(tmp_path_factory):
    db = Database(str(tmp_path_factory.mktemp("capacity-http") / "readings.db"))
    db.initialize()
    now = datetime.now(timezone.utc)
    for provider, percent in ((Provider.CODEX, 25), (Provider.CLAUDE, 100)):
        db.store_reading(Reading(
            provider=provider, status=ReadingStatus.CURRENT,
            session_percent=percent, session_resets_at=now + timedelta(hours=1),
            weekly_percent=None, weekly_resets_at=None, fetched_at=now, stale=False,
            detail="provider-private-diagnostic",
        ))
    app = create_app(
        "integration-operator", db,
        configured_providers=[Provider.CODEX, Provider.CLAUDE, Provider.UMANS],
        agent_api_key="integration-agent",
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "isolated capacity server did not start"
        yield origin
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        if not thread.is_alive():
            db._conn.close()
        assert not thread.is_alive(), "isolated capacity server did not stop"


def run_capacity(origin, *args, token="integration-agent"):
    return subprocess.run(
        [sys.executable, "-m", "usage_dashboard.cli", "capacity", *args],
        env={
            **os.environ,
            "USAGE_DASHBOARD_URL": origin,
            "USAGE_DASHBOARD_AGENT_TOKEN": token,
        },
        capture_output=True, text=True, timeout=20,
    )


def test_cli_consumes_real_api_contract(capacity_server):
    result = run_capacity(capacity_server)
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert body["schema_version"] == "agent-capacity-v1"
    assert body["job_fit"] == "not_assessed"
    accounts = {account["account_id"]: account for account in body["accounts"]}
    assert accounts["codex"]["assessment"] == "no_known_quota_block"
    assert accounts["codex"]["windows"][0]["remaining_percent"] == 75
    assert accounts["claude"]["assessment"] == "known_blocked"
    assert accounts["umans"]["freshness"] == "missing"
    assert accounts["umans"]["assessment"] == "unknown"
    assert "provider-private-diagnostic" not in result.stdout
    assert "integration-agent" not in result.stdout + result.stderr


def test_cli_filters_real_accounts_and_preserves_unknown(capacity_server):
    result = run_capacity(capacity_server + "/api/v1/", "--account", "umans")
    assert result.returncode == 0, result.stderr
    accounts = json.loads(result.stdout)["accounts"]
    assert len(accounts) == 1
    assert accounts[0]["account_id"] == "umans"
    assert accounts[0]["reasons"] == ["no_reading"]


@pytest.mark.parametrize("args,token,status", [
    ((), "invalid-agent", 401),
    (("--account", "ollama"), "integration-agent", 404),
    (("--max-age-seconds", "0"), "integration-agent", 422),
])
def test_cli_real_http_errors_are_nonzero_and_keep_stdout_clean(
    capacity_server, args, token, status,
):
    result = run_capacity(capacity_server, *args, token=token)
    assert result.returncode == 1
    assert result.stdout == ""
    assert f"HTTP {status}" in result.stderr
    assert token not in result.stderr


def test_read_only_key_cannot_refresh_real_server(capacity_server):
    # Without a scheduler an authorized request gets 501; 401 here proves
    # credential rejection precedes entry into the operator handler.
    with httpx.Client(base_url=capacity_server, timeout=5) as client:
        for prefix in ("", "/api/v1"):
            response = client.post(
                prefix + "/refresh", headers={"Authorization": "Bearer integration-agent"},
            )
            assert response.status_code == 401
        response = client.post(
            "/api/v1/refresh", headers={"Authorization": "Bearer integration-operator"},
        )
        assert response.status_code == 501
