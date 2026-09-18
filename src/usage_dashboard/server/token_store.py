"""Persistent token storage for OAuth credentials.

Tokens are saved as a JSON file on the PVC (alongside the SQLite DB) so that
rotated refresh tokens survive pod restarts.  This solves WI-001: without
persistence, a pod restart reverts to the stale refresh token in the k8s Secret,
which fails once (401) then goes offline.

**Cross-process safety (Plan 005).** Enrolment (``usage-dashboard login …``)
runs as a second process inside the server pod and mutates the same file the
running server writes on every token rotation. A thread lock cannot order two
processes, so every access takes an advisory ``flock`` on a sidecar lock file
and re-reads the file underneath it. Mutations are read-modify-write: reload,
apply one change, write a 0600 temp file, fsync, atomically replace, then
fsync the directory. That keeps an enrolment writing ``claude`` from
clobbering a rotation the server just wrote to ``codex``.

**Malformed content fails closed.** Unreadable JSON used to be swallowed,
leaving an empty in-memory store whose next write replaced the file — turning
a transient read problem into permanent credential loss. A corrupt store now
raises :class:`TokenStoreError` instead.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

_DEFAULT_TOKEN_PATH = "/data/tokens.json"


class TokenStoreError(Exception):
    """The on-disk token store is unusable and must not be overwritten."""


class TokenStore:
    """Process-safe persistent store for OAuth token pairs.

    Reads/writes a JSON file at *path*.  Each provider's tokens are stored
    under a top-level key (e.g. ``"claude"``) with ``access_token`` and
    ``refresh_token`` fields.
    """

    def __init__(self, path: str | Path = _DEFAULT_TOKEN_PATH) -> None:
        self._path = Path(path)
        self._lock_path = self._path.with_name(self._path.name + ".lock")
        self._thread_lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] = {}
        # Surface a corrupt store at construction rather than at the first
        # write, when it would already be too late to avoid clobbering it.
        with self._thread_lock, self._flock(exclusive=False):
            self._data = self._read()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, provider: str) -> tuple[str | None, str | None]:
        """Return (access_token, refresh_token) for *provider*, or (None, None)."""
        entry = self._entry(provider)
        access, refresh = entry.get("access_token"), entry.get("refresh_token")
        return (
            access if isinstance(access, str) else None,
            refresh if isinstance(refresh, str) else None,
        )

    def save(
        self,
        provider: str,
        access_token: str,
        refresh_token: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Persist a token pair for *provider*, preserving its other fields.

        *metadata* carries optional OAuth detail alongside the pair (e.g. a
        client id or expiry emitted by an official credential store). Keys with
        a ``None`` value are removed, so a credential that no longer carries a
        client id does not leave a stale one behind.
        """

        def mutate(data: dict[str, dict[str, Any]]) -> None:
            entry = data.setdefault(provider, {})
            entry["access_token"] = access_token
            entry["refresh_token"] = refresh_token
            for key, value in (metadata or {}).items():
                if value is None:
                    entry.pop(key, None)
                else:
                    entry[key] = value

        self._mutate(mutate)

    def get_metadata(self, provider: str, key: str) -> Any:
        """Return one optional metadata field for *provider*, or None."""
        return self._entry(provider).get(key)

    def save_credential(self, provider: str, credential: str) -> None:
        """Persist a single opaque credential for *provider* (preserving any seed marker)."""

        def mutate(data: dict[str, dict[str, Any]]) -> None:
            data.setdefault(provider, {})["credential"] = credential

        self._mutate(mutate)

    def get_credential(self, provider: str) -> str | None:
        """Return the stored credential for *provider*, or None."""
        value = self._entry(provider).get("credential")
        return value if isinstance(value, str) else None

    def get_seed_marker(self, provider: str) -> str | None:
        """Return the marker recorded for the credentials last seeded from the
        environment for *provider* (used to detect a changed Secret)."""
        value = self._entry(provider).get("seed_marker")
        return value if isinstance(value, str) else None

    def set_seed_marker(self, provider: str, marker: str) -> None:
        """Record the seed marker for *provider* without touching its tokens."""

        def mutate(data: dict[str, dict[str, Any]]) -> None:
            data.setdefault(provider, {})["seed_marker"] = marker

        self._mutate(mutate)

    def load_claude_tokens(self) -> tuple[str | None, str | None]:
        """Convenience: return (access, refresh) for the 'claude' provider."""
        return self.get("claude")

    def save_claude_tokens(self, access_token: str, refresh_token: str) -> None:
        """Convenience: persist tokens for the 'claude' provider."""
        self.save("claude", access_token, refresh_token)

    def get_claude_seed_marker(self) -> str | None:
        return self.get_seed_marker("claude")

    def set_claude_seed_marker(self, marker: str) -> None:
        self.set_seed_marker("claude", marker)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _entry(self, provider: str) -> dict[str, Any]:
        """Reload under a shared lock and return *provider*'s entry.

        Reads reload so a credential written by the enrolment process is
        visible to the running server without a restart.
        """
        with self._thread_lock, self._flock(exclusive=False):
            self._data = self._read()
            entry = self._data.get(provider, {})
            return dict(entry) if isinstance(entry, dict) else {}

    def _mutate(self, apply: Callable[[dict[str, dict[str, Any]]], None]) -> None:
        """Read-modify-write the store atomically under an exclusive lock."""
        with self._thread_lock, self._flock(exclusive=True):
            data = self._read()
            apply(data)
            self._write(data)
            self._data = data

    @contextmanager
    def _flock(self, *, exclusive: bool) -> Iterator[None]:
        """Advisory lock on a sidecar file, so the token file itself is only
        ever replaced atomically (a lock held on it would follow the old inode).
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            # An unlockable store still has to work read-only in tests and in
            # a read-only mount; degrade to the in-process lock alone.
            logger.warning("Token store lock unavailable at %s: %s", self._lock_path, exc)
            yield
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _read(self) -> dict[str, dict[str, Any]]:
        """Load the store from disk, or raise if it is unusable."""
        if not self._path.exists():
            return {}
        try:
            with open(self._path) as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise TokenStoreError(
                f"Token store {self._path} is not valid JSON ({exc}); refusing to "
                "overwrite it. Move it aside to start fresh."
            ) from exc
        except OSError as exc:
            raise TokenStoreError(f"Token store {self._path} could not be read: {exc}") from exc
        if not isinstance(data, dict):
            raise TokenStoreError(
                f"Token store {self._path} is not a JSON object; refusing to overwrite it."
            )
        return data

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        """Atomically replace the store, durably.

        The temp file is per-process: the exclusive lock normally serialises
        writers, but :meth:`_flock` degrades to a no-op when the directory
        cannot host a lock file, and a shared ``tokens.tmp`` would then let two
        writers interleave into one temp file and rename a torn result into
        place.
        """
        tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(self._path)
            # fsync the directory so the rename itself survives a crash.
            dir_fd = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            logger.error("Failed to persist tokens to %s: %s", self._path, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise TokenStoreError(f"Token store {self._path} could not be written: {exc}") from exc
