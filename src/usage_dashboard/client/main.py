from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from typing import Any

from usage_dashboard.client.fetcher import ClientFetcher
from usage_dashboard.client.renderer import DisplayRenderer

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    server_url = os.environ.get("SERVER_URL", "")
    if not server_url:
        logger.error("SERVER_URL environment variable is required")
        sys.exit(1)

    api_key = os.environ.get("API_KEY", "")
    if not api_key:
        logger.error("API_KEY environment variable is required")
        sys.exit(1)

    fetcher = ClientFetcher(server_url=server_url, api_key=api_key)
    renderer = DisplayRenderer()
    display_path = os.environ.get("DISPLAY_OUTPUT", "/tmp/dashboard.png")

    display_dir = os.path.dirname(display_path)
    if display_dir:
        os.makedirs(display_dir, exist_ok=True)

    shutdown_event = threading.Event()

    def _handle_sigterm(signum: int, frame: Any) -> None:
        logger.info("Received SIGTERM, shutting down")
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _handle_sigterm)

    fetcher.start()
    logger.info("Client started, polling %s", server_url)

    try:
        while not shutdown_event.is_set():
            readings = fetcher.get_latest_readings()
            if readings:
                img = renderer.render(readings)
                img.save(display_path)
            shutdown_event.wait(timeout=5.0)
    except KeyboardInterrupt:
        pass
    finally:
        fetcher.stop()
        logger.info("Client stopped")


if __name__ == "__main__":
    main()
