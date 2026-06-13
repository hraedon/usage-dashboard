from __future__ import annotations

import logging
import os
import sys

import uvicorn

from usage_dashboard.server.api import create_app
from usage_dashboard.server.db import Database
from usage_dashboard.server.scheduler import FetchScheduler
from usage_dashboard.server.token_store import TokenStore

logger = logging.getLogger(__name__)


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
    zai_api_key = os.environ.get("ZAI_API_KEY") or None
    ollama_cookie = os.environ.get("OLLAMA_COOKIE") or None
    umans_api_key = os.environ.get("UMANS_API_KEY") or None
    fetch_interval = int(os.environ.get("FETCH_INTERVAL", "300"))
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

    # Seed the store from env vars if present (first boot or Secret update).
    if claude_token and claude_refresh_token:
        token_store.save_claude_tokens(claude_token, claude_refresh_token)
    # Fall back to persisted tokens when env vars are empty (restart after
    # the initial login, where the Secret may not have been updated yet).
    if not claude_token or not claude_refresh_token:
        persisted_access, persisted_refresh = token_store.load_claude_tokens()
        claude_token = claude_token or persisted_access
        claude_refresh_token = claude_refresh_token or persisted_refresh

    scheduler = FetchScheduler(
        db=database,
        claude_token=claude_token,
        claude_refresh_token=claude_refresh_token,
        claude_client_id=claude_client_id,
        zai_key=zai_api_key,
        ollama_cookie=ollama_cookie,
        umans_key=umans_api_key,
        interval_seconds=fetch_interval,
        token_store=token_store,
    )

    app = create_app(api_key=api_key, db=database)

    scheduler.start()

    try:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
    finally:
        scheduler.stop()


if __name__ == "__main__":
    main()
