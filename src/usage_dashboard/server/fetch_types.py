from __future__ import annotations

import json
import logging
import os

_logger = logging.getLogger(__name__)


def debug_dump(name: str, payload: str) -> None:
    """Write a raw provider payload to disk for one-time schema inspection.

    Temporary diagnostic: set ``USAGE_DEBUG_DUMP=/some/dir`` to capture the raw
    response body from each fetcher. Disabled (no-op) when the env var is unset,
    so it has zero effect on a production deployment that doesn't opt in.
    """
    dump_dir = os.environ.get("USAGE_DEBUG_DUMP")
    if not dump_dir:
        return
    try:
        os.makedirs(dump_dir, exist_ok=True)
        path = os.path.join(dump_dir, name)
        with open(path, "w") as fh:
            fh.write(payload)
        _logger.info("Debug dump written: %s (%d bytes)", path, len(payload))
    except OSError as exc:
        _logger.warning("Debug dump failed for %s: %s", name, exc)


def dump_json(name: str, data: object) -> None:
    """Convenience wrapper: serialize *data* as indented JSON then dump."""
    debug_dump(name, json.dumps(data, indent=2, default=str))


class FetchError(Exception):
    pass


class FetchAuthError(FetchError):
    """Fetch failed because the credential was rejected (401/403).

    Distinct from FetchError so the scheduler only attempts token refresh
    when the credential is actually the problem — refreshing on transient
    failures (429s, timeouts) hammers the OAuth endpoint for nothing.
    """


class FetchRateLimitError(FetchError):
    """Fetch was rate limited (429); the scheduler should back off.

    retry_after_seconds comes from the Retry-After header when present so
    the scheduler can skip the provider until the window passes instead of
    re-polling into the same limit every cycle.
    """

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
