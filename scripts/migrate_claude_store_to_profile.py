"""One-off Plan 009 migration: move a Claude credential from the token store
into the account's CLI profile, so the upgrade needs no re-login.

Run it inside the server pod **after** the Plan 009 image is rolled out:

    kubectl -n usage-dashboard exec -i deploy/usage-dashboard-server -- \
        python - < scripts/migrate_claude_store_to_profile.py

Order matters, for two reasons. The script imports Plan 009 modules, which
only the new image has. And the deployment's ``Recreate`` strategy means no
old server survives the rollout, so nothing else holds this refresh-token
family once the new pod is up: the profile's CLI becomes its only refresher.
Until the script runs, the new server keeps the account configured, and the
tile reads "login required" rather than disappearing. Afterwards the script
asks the server to fetch (``POST /refresh``), so the tile recovers at once.

The file written is the minimal shape WP-1 verified Claude Code 2.1.276
accepts and then refreshes: the pair, expiry, scopes and plan. The CLI
rewrites it in full on its first refresh. If the stored access token has
already expired (or is rejected), it is written as expired, and the first
fetch makes the CLI refresh with the stored refresh token. This is
deliberately a script, not server code: nothing in ``src/`` writes the CLI's
credential format.

The store entry is NOT deleted here. The server prunes it only after the
CLI's first refresh has rotated the refresh token, so until then it remains a
fallback copy of a working pair.

Prints no token material. Idempotent: an account whose profile already holds
a credential is skipped.
"""
from __future__ import annotations

import json
import os
import shutil
import time

import httpx

from usage_dashboard.claude_credentials import parse_credentials
from usage_dashboard.server.claude_profile import (
    ACCOUNT_PROVIDERS,
    credentials_path,
    is_enrolled,
    profile_dir,
)
from usage_dashboard.server.cli_home import install_home, make_staging_home
from usage_dashboard.server.fetch_claude import fetch_claude_usage
from usage_dashboard.server.fetch_types import FetchAuthError, FetchError
from usage_dashboard.server.token_store import TokenStore


class MigrationError(Exception):
    """This account could not be migrated; nothing was changed."""


def build_payload(store: TokenStore, key: str) -> dict[str, object] | None:
    """The CLI credential file for *key*'s stored pair, or None if none."""
    access, refresh = store.get(key)
    if not (access and refresh):
        return None
    expires_at = store.get_metadata(key, "expires_at")
    now_ms = int(time.time() * 1000)
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        expires_at = 0  # unknown: the CLI refreshes on first use
    if expires_at > now_ms:
        try:
            fetch_claude_usage(access, ACCOUNT_PROVIDERS[key])
        except FetchAuthError:
            # Rejected early. The refresh token may still be good; only the
            # CLI can find out, so mark the access token expired.
            expires_at = 0
        except FetchError as exc:
            raise MigrationError(f"could not verify the stored pair ({exc}); retry") from exc
    oauth: dict[str, object] = {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresAt": expires_at,
    }
    scopes = store.get_metadata(key, "scopes")
    if isinstance(scopes, list):
        oauth["scopes"] = scopes
    plan = store.get_metadata(key, "subscription_type")
    if isinstance(plan, str):
        oauth["subscriptionType"] = plan
    payload: dict[str, object] = {"claudeAiOauth": oauth}
    parse_credentials(payload)  # the reader must accept what we write
    return payload


def migrate(store: TokenStore, key: str) -> str:
    target = profile_dir(key)
    if is_enrolled(target):
        return f"profile {target} already holds a credential; skipped"
    payload = build_payload(store, key)
    if payload is None:
        return "no store credential; nothing to migrate"
    staging = make_staging_home(target)
    try:
        fd = os.open(credentials_path(staging), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        install_home(staging, target)
        staging = ""
    finally:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
    return f"migrated into {target}"


def request_fetch() -> str:
    """Ask the local server to fetch now, so the tile recovers immediately."""
    api_key = os.environ.get("API_KEY")
    if not api_key:
        return "API_KEY unset; the next poll (or POST /refresh) picks it up"
    port = os.environ.get("PORT", "8080")
    try:
        response = httpx.post(
            f"http://127.0.0.1:{port}/refresh",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=120.0,
        )
    except httpx.HTTPError as exc:
        return f"POST /refresh failed ({type(exc).__name__}); refresh from a panel"
    return f"POST /refresh -> HTTP {response.status_code}"


def main() -> None:
    store = TokenStore(os.environ.get("TOKEN_STORE_PATH") or "/data/tokens.json")
    migrated = False
    for key in ACCOUNT_PROVIDERS:
        try:
            result = migrate(store, key)
        except MigrationError as exc:
            result = f"not migrated: {exc}"
        migrated = migrated or result.startswith("migrated")
        print(f"{key}: {result}")
    if migrated:
        print(request_fetch())


if __name__ == "__main__":
    main()
