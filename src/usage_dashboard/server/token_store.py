"""Persistent token storage for OAuth credentials.

Tokens are saved as a JSON file on the PVC (alongside the SQLite DB) so that
rotated refresh tokens survive pod restarts.  This solves WI-001: without
persistence, a pod restart reverts to the stale refresh token in the k8s Secret,
which fails once (401) then goes offline.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_TOKEN_PATH = "/data/tokens.json"


class TokenStore:
    """Thread-safe persistent store for OAuth token pairs.

    Reads/writes a JSON file at *path*.  Each provider's tokens are stored
    under a top-level key (e.g. ``"claude"``) with ``access_token`` and
    ``refresh_token`` fields.
    """

    def __init__(self, path: str | Path = _DEFAULT_TOKEN_PATH) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, str]] = {}
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, provider: str) -> tuple[str | None, str | None]:
        """Return (access_token, refresh_token) for *provider*, or (None, None)."""
        with self._lock:
            entry = self._data.get(provider, {})
            return entry.get("access_token"), entry.get("refresh_token")

    def save(self, provider: str, access_token: str, refresh_token: str) -> None:
        """Persist a token pair for *provider*."""
        with self._lock:
            self._data[provider] = {
                "access_token": access_token,
                "refresh_token": refresh_token,
            }
            self._flush()

    def load_claude_tokens(self) -> tuple[str | None, str | None]:
        """Convenience: return (access, refresh) for the 'claude' provider."""
        return self.get("claude")

    def save_claude_tokens(self, access_token: str, refresh_token: str) -> None:
        """Convenience: persist tokens for the 'claude' provider."""
        self.save("claude", access_token, refresh_token)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path) as f:
                self._data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load token store from %s: %s", self._path, exc)

    def _flush(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(self._data, f, indent=2)
            tmp.replace(self._path)
        except OSError as exc:
            logger.error("Failed to persist tokens to %s: %s", self._path, exc)
