"""One-off Plan 009 migration: move a Claude credential from the token store
into the account's CLI profile, so the upgrade needs no re-login.

Run inside the server pod, immediately before rolling out the Plan 009 image:

    kubectl -n usage-dashboard exec -i deploy/usage-dashboard-server -- \
        python - < scripts/migrate_claude_store_to_profile.py

then roll the deployment straight away. Until the rollout the old server still
holds the same pair and would refresh it on a 401, which would strand the
profile's copy; the script therefore refuses when the access token is within
30 minutes of expiry (pass --force to override). After the rollout the new
server finds the profile, removes the store entry, and the CLI is the
family's only refresher.

The file written is the minimal shape WP-1 verified that Claude Code 2.1.276
accepts and then refreshes: the pair, expiry, scopes and plan. The CLI
rewrites it in full on its first refresh. This is deliberately a script, not
server code: nothing in ``src/`` writes the CLI's credential format.

Prints no token material. Idempotent: an account whose profile already holds
a credential is skipped.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time

from usage_dashboard.claude_credentials import parse_credentials
from usage_dashboard.server.claude_profile import (
    ACCOUNT_PROVIDERS,
    credentials_path,
    is_enrolled,
    profile_dir,
)
from usage_dashboard.server.cli_home import install_home, make_staging_home
from usage_dashboard.server.fetch_claude import fetch_claude_usage
from usage_dashboard.server.fetch_types import FetchError
from usage_dashboard.server.token_store import TokenStore

_MIN_REMAINING_MS = 30 * 60 * 1000


def migrate(store: TokenStore, key: str, force: bool) -> str:
    access, refresh = store.get(key)
    if not (access and refresh):
        return "no store credential; nothing to migrate"
    target = profile_dir(key)
    if is_enrolled(target):
        return f"profile {target} already holds a credential; skipped"

    expires_at = store.get_metadata(key, "expires_at")
    now_ms = int(time.time() * 1000)
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        # Unknown: record it as expired, so the CLI refreshes on first use.
        expires_at = 0
    elif 0 < expires_at - now_ms < _MIN_REMAINING_MS and not force:
        raise SystemExit(
            f"{key}: access token expires in under 30 minutes; the old server "
            "may refresh it before the rollout. Wait for its refresh, or pass --force."
        )
    if expires_at > now_ms:
        # Prove the pair still works before making it the profile.
        fetch_claude_usage(access, ACCOUNT_PROVIDERS[key])

    oauth = {"accessToken": access, "refreshToken": refresh, "expiresAt": expires_at}
    scopes = store.get_metadata(key, "scopes")
    if isinstance(scopes, list):
        oauth["scopes"] = scopes
    plan = store.get_metadata(key, "subscription_type")
    if isinstance(plan, str):
        oauth["subscriptionType"] = plan
    payload = {"claudeAiOauth": oauth}
    parse_credentials(payload)  # the reader must accept what we write

    staging = make_staging_home(target)
    try:
        path = credentials_path(staging)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        install_home(staging, target)
        staging = ""
    finally:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
    return f"migrated into {target}; roll the server now"


def main() -> None:
    force = "--force" in sys.argv[1:]
    store = TokenStore(os.environ.get("TOKEN_STORE_PATH") or "/data/tokens.json")
    for key in ACCOUNT_PROVIDERS:
        try:
            print(f"{key}: {migrate(store, key, force)}")
        except FetchError as exc:
            print(f"{key}: the stored pair cannot read usage ({exc}); re-enrol instead")


if __name__ == "__main__":
    main()
