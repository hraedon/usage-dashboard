from __future__ import annotations

import logging
import os
import sys

import uvicorn

from usage_dashboard.server.api import create_app
from usage_dashboard.server.db import Database
from usage_dashboard.server.scheduler import FetchScheduler

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
    ollama_email = os.environ.get("OLLAMA_EMAIL") or None
    ollama_password = os.environ.get("OLLAMA_PASSWORD") or None
    umans_api_key = os.environ.get("UMANS_API_KEY") or None
    if umans_api_key is None:
        # Interim until the key moves into the k8s Secret
        umans_key_file = os.path.expanduser(
            os.environ.get("UMANS_API_KEY_FILE", "~/umans_api.txt")
        )
        if os.path.isfile(umans_key_file):
            with open(umans_key_file, encoding="utf-8") as f:
                umans_api_key = f.read().strip() or None
    fetch_interval = int(os.environ.get("FETCH_INTERVAL", "300"))
    port = int(os.environ.get("PORT", "8080"))

    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    database = Database(db_path)
    database.initialize()

    scheduler = FetchScheduler(
        db=database,
        claude_token=claude_token,
        claude_refresh_token=claude_refresh_token,
        claude_client_id=claude_client_id,
        zai_key=zai_api_key,
        ollama_email=ollama_email,
        ollama_password=ollama_password,
        umans_key=umans_api_key,
        interval_seconds=fetch_interval,
    )

    app = create_app(api_key=api_key, db=database)

    scheduler.start()

    try:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
    finally:
        scheduler.stop()


if __name__ == "__main__":
    main()
