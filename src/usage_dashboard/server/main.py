from __future__ import annotations

import hashlib
import logging
import os
import sys

import uvicorn

from usage_dashboard.server.api import create_app
from usage_dashboard.server.db import Database
from usage_dashboard.server.scheduler import FetchScheduler
from usage_dashboard.server.token_store import TokenStore

logger = logging.getLogger(__name__)


def _resolve_claude_tokens(
    token_store: TokenStore,
    env_access: str | None,
    env_refresh: str | None,
    store_key: str = "claude",
) -> tuple[str | None, str | None]:
    """Decide which Claude tokens to run with (WI-001).

    The k8s Secret keeps the originally-provisioned tokens forever, so seeding
    the store from the env on every boot would clobber the refreshed tokens the
    scheduler persisted — after a restart the stale pair 401s and its
    already-rotated refresh token can't recover. Instead: only (re)seed from the
    env when the Secret differs from what we last seeded (first boot or a
    deliberate re-login); otherwise prefer the persisted, possibly-refreshed
    tokens. A hash of the env access token is the change marker, so the Secret
    value isn't duplicated on disk.

    *store_key* namespaces the credentials so a second account ("claude_work")
    resolves and persists independently of the primary one.
    """
    persisted_access, persisted_refresh = token_store.get(store_key)

    if env_access and env_refresh:
        marker = hashlib.sha256(env_access.encode()).hexdigest()
        if marker != token_store.get_seed_marker(store_key):
            # New credential from the Secret — adopt it and record the marker.
            token_store.save(store_key, env_access, env_refresh)
            token_store.set_seed_marker(store_key, marker)
            return env_access, env_refresh

    # Prefer persisted (refreshed) tokens; fall back to whatever the env gave.
    return persisted_access or env_access, persisted_refresh or env_refresh


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    api_key = os.environ.get("API_KEY", "")
    if not api_key:
        logger.error("API_KEY environment variable is required")
        sys.exit(1)

    db_path = os.environ.get("DB_PATH", "/data/readings.db")
    claude_token = os.environ.get("CLAUDE_TOKEN") or None
    claude_refresh_token = os.environ.get("CLAUDE_REFRESH_TOKEN") or None
    claude_client_id = os.environ.get("CLAUDE_CLIENT_ID") or None
    # Optional second Claude account (e.g. a work login). Absent unless its own
    # token is set — the dashboard then shows a muted second set of Claude bars.
    claude_work_token = os.environ.get("CLAUDE_WORK_TOKEN") or None
    claude_work_refresh_token = os.environ.get("CLAUDE_WORK_REFRESH_TOKEN") or None
    claude_work_client_id = os.environ.get("CLAUDE_WORK_CLIENT_ID") or None
    zai_api_key = os.environ.get("ZAI_API_KEY") or None
    ollama_cookie = os.environ.get("OLLAMA_COOKIE") or None
    umans_api_key = os.environ.get("UMANS_API_KEY") or None
    fetch_interval = int(os.environ.get("FETCH_INTERVAL", "300"))
    failure_backoff_cap = int(os.environ.get("FAILURE_BACKOFF_CAP", "3600"))
    port = int(os.environ.get("PORT", "8080"))

    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    database = Database(db_path)
    database.initialize()

    # Token store lives on the PVC alongside the DB.  Env-var tokens (from
    # the k8s Secret) seed it on first boot; refreshed tokens persist here
    # so pod restarts survive a rotation without touching the Secret.
    token_store = TokenStore(os.path.join(db_dir or "/data", "tokens.json"))

    claude_token, claude_refresh_token = _resolve_claude_tokens(
        token_store, claude_token, claude_refresh_token
    )
    claude_work_token, claude_work_refresh_token = _resolve_claude_tokens(
        token_store, claude_work_token, claude_work_refresh_token,
        store_key="claude_work",
    )

    scheduler = FetchScheduler(
        db=database,
        claude_token=claude_token,
        claude_refresh_token=claude_refresh_token,
        claude_client_id=claude_client_id,
        claude_work_token=claude_work_token,
        claude_work_refresh_token=claude_work_refresh_token,
        claude_work_client_id=claude_work_client_id,
        zai_key=zai_api_key,
        ollama_cookie=ollama_cookie,
        umans_key=umans_api_key,
        interval_seconds=fetch_interval,
        failure_cap_seconds=failure_backoff_cap,
        token_store=token_store,
    )

    app = create_app(
        api_key=api_key,
        db=database,
        configured_providers=scheduler.configured_providers(),
    )

    scheduler.start()

    try:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
    finally:
        scheduler.stop()


if __name__ == "__main__":
    main()
