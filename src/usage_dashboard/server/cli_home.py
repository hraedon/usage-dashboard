"""One owner of a vendor CLI's state directory (Plans 007 and 009).

Antigravity's ``HOME`` and Claude Code's ``CLAUDE_CONFIG_DIR`` are both
directories the official CLI owns outright: the login lives in a file inside,
and the CLI rewrites it on refresh. Enrolment signs in inside a *staging*
sibling and then swaps it into place, so a failed or abandoned ceremony never
displaces a working login. The fetcher and the swap share an advisory lock so
a poll never runs against a half-moved directory.

POSIX-only (``fcntl``), like the rest of the server.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager

# ---------------------------------------------------------------------------
# Locking and the staging swap
#
# Enrolment signs in inside a staging directory and then swaps it into place.
# The fetcher and the swap share an advisory lock so a poll never runs against
# a half-moved directory. It is a *file* lock because the `kubectl exec`
# enrolment path runs in a second process. The lock file sits beside the home
# rather than inside it, since the home itself is what gets replaced.
# ---------------------------------------------------------------------------


@contextmanager
def home_lock(home: str) -> Iterator[None]:
    parent = os.path.dirname(os.path.abspath(home))
    os.makedirs(parent, exist_ok=True)
    fd = os.open(f"{os.path.abspath(home)}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _prev_dir(home: str) -> str:
    return f"{os.path.abspath(home)}.prev"


def _swap_marker(home: str) -> str:
    return f"{os.path.abspath(home)}.swapping"


def recover_interrupted_swap(home: str) -> None:
    """Settle a swap that died part-way (caller holds the lock).

    Recovery keys on the marker, never on a missing home alone: deleting
    ``home`` is how an operator signs out, and that must not resurrect the
    old login from a leftover ``.prev``.
    """
    home = os.path.abspath(home)
    prev = _prev_dir(home)
    marker = _swap_marker(home)
    if os.path.exists(marker):
        if not os.path.exists(home) and os.path.isdir(prev):
            # Died between the two renames: the old login is the only one.
            os.rename(prev, home)
        os.unlink(marker)
    if os.path.exists(prev):
        # Died after the swap completed, before the old login was removed.
        shutil.rmtree(prev, ignore_errors=True)


def make_staging_home(home: str) -> str:
    """A fresh 0700 sibling of *home* for a sign-in ceremony.

    It has to be a sibling so that the final swap is a same-filesystem rename.
    """
    parent = os.path.dirname(os.path.abspath(home))
    os.makedirs(parent, exist_ok=True)
    prefix = os.path.basename(os.path.abspath(home)) + ".enrol-"
    _sweep_stale_staging(parent, prefix)
    return tempfile.mkdtemp(prefix=prefix, dir=parent)


# A staging dir older than this belongs to a ceremony that was killed outright
# (the CLI's own finally removes it otherwise). Antigravity's login window is
# 60 s and a Claude login takes a minute or two, so an hour leaves a wide
# margin for one that is still running.
STALE_STAGING_SECONDS = 3600.0


def _sweep_stale_staging(parent: str, prefix: str) -> None:
    cutoff = time.time() - STALE_STAGING_SECONDS
    try:
        entries = os.listdir(parent)
    except OSError:
        return
    for name in entries:
        path = os.path.join(parent, name)
        if not name.startswith(prefix) or not os.path.isdir(path):
            continue
        try:
            if os.stat(path).st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def install_home(staging: str, home: str) -> None:
    """Make a verified staging directory the live home.

    The previous home survives as ``<home>.prev`` until the new one is in
    place, so a crash mid-swap is undone by the next fetch, not lost.
    """
    home = os.path.abspath(home)
    prev = _prev_dir(home)
    marker = _swap_marker(home)
    with home_lock(home):
        recover_interrupted_swap(home)
        had_home = os.path.exists(home)
        if had_home:
            with open(marker, "w"):
                pass
            os.rename(home, prev)
        os.rename(staging, home)
        if had_home:
            os.unlink(marker)
            shutil.rmtree(prev, ignore_errors=True)
