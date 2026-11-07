"""The `usage-dashboard capacity` command — read-only agent capacity client.

Portability contract: this module, and everything it imports, must import
cleanly on any OS an agent may run on (Windows workstations included). The
enrolment commands in ``usage_dashboard.cli`` are deliberately POSIX-bound
(pty/fcntl, browser ceremonies on the server pod); this command is the one
agents are told to call, so it carries no such baggage.

Moved out of cli.py on 2026-11-07: a module-level import chain through
server.fetch_antigravity (fcntl) made even this command fail to import on
Windows (PR 37 deployment round).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from urllib.parse import urlparse

import httpx

_CAPACITY_TIMEOUT = 15.0
_CAPACITY_PATH = "/api/v1/agent/capacity"
_CAPACITY_SCHEMA = "agent-capacity-v1"


class _CapacityCommandError(Exception):
    """A safe, user-facing capacity command failure."""


def _capacity_url(raw_url: str) -> str:
    """Build the capacity endpoint from a dashboard origin.

    The documented value is an origin, but accepting a trailing ``/api/v1``
    makes the command less surprising when copied from an API URL. Other
    paths, credentials, schemes, query strings and fragments are rejected.
    """
    value = raw_url.strip()
    if not value:
        raise _CapacityCommandError("USAGE_DASHBOARD_URL is required")
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        parsed.port  # Validate malformed ports while the URL is available.
    except ValueError as exc:
        raise _CapacityCommandError("USAGE_DASHBOARD_URL is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise _CapacityCommandError("USAGE_DASHBOARD_URL must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise _CapacityCommandError("USAGE_DASHBOARD_URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise _CapacityCommandError("USAGE_DASHBOARD_URL must not contain a query or fragment")
    path = parsed.path.rstrip("/")
    if path not in {"", "/api/v1"}:
        raise _CapacityCommandError("USAGE_DASHBOARD_URL must be an origin or end in /api/v1")
    return f"{parsed.scheme.lower()}://{parsed.netloc}{_CAPACITY_PATH}"


def _fetch_capacity(
    endpoint: str,
    token: str,
    account: str | None,
    max_age_seconds: int | None,
) -> dict[str, Any]:
    params: dict[str, str | int] = {}
    if account is not None:
        params["account"] = account
    if max_age_seconds is not None:
        params["max_age_seconds"] = max_age_seconds
    try:
        with httpx.Client(timeout=_CAPACITY_TIMEOUT) as client:
            response = client.get(
                endpoint,
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError) as exc:
        # URL/header construction can fail before HTTPX creates a request.
        # Keep those diagnostics out of stderr too: they may include the key.
        raise _CapacityCommandError("capacity request failed") from exc

    if response.status_code != 200:
        raise _CapacityCommandError(f"capacity server returned HTTP {response.status_code}")
    try:
        body = response.json()
    except (TypeError, ValueError) as exc:
        raise _CapacityCommandError("capacity server returned invalid JSON") from exc
    if not isinstance(body, dict):
        raise _CapacityCommandError("capacity server returned an unsupported response")
    if body.get("schema_version") != _CAPACITY_SCHEMA:
        raise _CapacityCommandError("capacity server returned an unsupported schema")
    return body


def capacity(
    account: str | None = None,
    max_age_seconds: int | None = None,
) -> int:
    """Print one authenticated capacity snapshot and return a process status."""
    dashboard_url = os.environ.get("USAGE_DASHBOARD_URL", "")
    token = os.environ.get("USAGE_DASHBOARD_AGENT_TOKEN", "")
    if not token:
        print("capacity: USAGE_DASHBOARD_AGENT_TOKEN is required", file=sys.stderr)
        return 1
    try:
        endpoint = _capacity_url(dashboard_url)
        snapshot = _fetch_capacity(endpoint, token, account, max_age_seconds)
    except _CapacityCommandError as exc:
        print(f"capacity: {exc}", file=sys.stderr)
        return 1
    json.dump(snapshot, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0
