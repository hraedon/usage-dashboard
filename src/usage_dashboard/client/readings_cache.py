"""The on-disk handoff between the cellular readings worker and the touch GUI
(Plan 010).

Two processes with different privileges need to agree on one payload. The
readings worker owns the metered link and the display credential; the GUI
renders, and for the cellular path must do so *without* any network privilege.
They never talk to each other directly. The worker is the single writer of
everything here except the two GUI signals, and the GUI only reads:

``<state dir>/readings.json``   the last payload read successfully from the
                                server, whichever route carried it
``<state dir>/status.json``     what the worker is doing and why (diagnostics)
``<state dir>/usage.json``      bytes counted against this month's data budget

``<state dir>/request/`` is the reverse channel, owned by the GUI user: the
worker cannot write there and can only read what the GUI leaves.

The contract this file exists for is "preserve the last successful payload and
its timestamp when both routes fail, and show its age on the panel". A payload
is therefore never cleared — not on error, not on shutdown — only replaced by a
newer successful read, and every replacement is a single atomic rename so the
GUI can never observe a half-written file.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from usage_dashboard.shared.models import Provider, Reading

logger = logging.getLogger(__name__)

CACHE_SCHEMA = "usage-dashboard-readings-cache-v1"
STATUS_SCHEMA = "usage-dashboard-cellular-status-v1"

READINGS_FILE = "readings.json"
STATUS_FILE = "status.json"
USAGE_FILE = "usage.json"
REQUEST_DIR = "request"
AWAKE_FILE = "awake"
REFRESH_FILE = "refresh"

# Which route carried a payload. "network" is Wi-Fi or Ethernet — the system's
# normal route — and "cellular" is the metered fallback.
SOURCE_NETWORK = "network"
SOURCE_CELLULAR = "cellular"

# What the worker is doing. These are part of the status file's contract: the
# GUI's diagnostics read them back.
STATE_IDLE = "idle"            # the normal route reaches the dashboard
STATE_FALLBACK = "fallback"    # the normal route is dead; cellular is in use
STATE_BUDGET = "budget"        # this month's data budget is spent
STATE_CLOCK = "clock"          # the clock is not set, so HTTPS cannot validate

_KNOWN_PROVIDERS = frozenset(p.value for p in Provider)


class CacheFormatError(ValueError):
    """A file on disk is not the format this build understands."""


def utc_naive_now() -> datetime:
    """Wall clock as naive UTC, the convention every ``Reading`` timestamp
    already uses."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def format_ts(value: datetime) -> str:
    """Render a datetime as the schema's UTC instant (``…Z``)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: object, field: str) -> datetime:
    """Parse a schema instant into naive UTC. Raises :class:`CacheFormatError`
    so a bad timestamp rejects the whole entry rather than silently becoming
    "now", which would make an old payload look fresh."""
    if not isinstance(value, str):
        raise CacheFormatError(f"{field} is not a string")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CacheFormatError(f"{field} is not an ISO instant: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def write_atomic(path: Path, text: str) -> None:
    """Replace *path* with *text* in one step.

    Readers either see the previous file or the new one, never a partial write:
    the content lands in a sibling temporary file, is flushed to the disk, and
    is renamed over the target. The rename is atomic on POSIX because both
    names share a directory (and therefore a filesystem).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        # A crashed writer must not leave dot-files accumulating in the state
        # dir, where nothing else would ever clean them up.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# -- readings cache ----------------------------------------------------------


@dataclass(frozen=True)
class CacheEntry:
    """One published payload.

    ``written_at`` is when the worker last read this payload from the server —
    the panel's "how old is this" reference. It is deliberately not the
    server-side ``fetched_at`` inside the readings: that is when the *provider*
    was polled, which can predate a successful read by any amount.
    """

    written_at: datetime
    source: str
    poll_interval: int
    raw: list[dict[str, Any]]

    def readings(self) -> list[Reading]:
        """Decode the payload for display.

        Providers this build does not know are skipped, exactly as the GUI's
        own fetcher does: a server can ship a new provider before every panel
        has updated, and one unknown row must not cost the panel every other
        reading. Anything else malformed raises, because rendering half a
        payload would hide the corruption.
        """
        readings: list[Reading] = []
        for item in self.raw:
            if not isinstance(item, dict):
                raise CacheFormatError("readings item is not an object")
            if item.get("provider") not in _KNOWN_PROVIDERS:
                continue
            try:
                readings.append(Reading.from_dict(item))
            except (KeyError, TypeError, ValueError) as exc:
                raise CacheFormatError(f"malformed reading: {exc}") from exc
        return readings


def encode_cache(entry: CacheEntry) -> str:
    return json.dumps(
        {
            "schema": CACHE_SCHEMA,
            "written_at": format_ts(entry.written_at),
            "source": entry.source,
            "poll_interval": entry.poll_interval,
            "readings": entry.raw,
        }
    )


def decode_cache(text: str) -> CacheEntry:
    """Parse a cache document; raises :class:`CacheFormatError` on anything this
    build cannot vouch for. Unknown schema versions are rejected rather than
    guessed at."""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CacheFormatError(f"not JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise CacheFormatError("cache is not an object")
    if doc.get("schema") != CACHE_SCHEMA:
        raise CacheFormatError(f"unsupported schema {doc.get('schema')!r}")
    readings = doc.get("readings")
    if not isinstance(readings, list) or not all(isinstance(r, dict) for r in readings):
        raise CacheFormatError("readings is not a list of objects")
    source = doc.get("source")
    if source not in (SOURCE_NETWORK, SOURCE_CELLULAR):
        raise CacheFormatError(f"unknown source {source!r}")
    poll_interval = doc.get("poll_interval")
    if not isinstance(poll_interval, int) or isinstance(poll_interval, bool) or poll_interval <= 0:
        raise CacheFormatError("poll_interval is not a positive integer")
    entry = CacheEntry(
        written_at=parse_ts(doc.get("written_at"), "written_at"),
        source=source,
        poll_interval=poll_interval,
        raw=readings,
    )
    # Decode once here so a structurally valid file with unusable content is
    # rejected by the writer's own rule, not discovered mid-frame by the GUI.
    entry.readings()
    return entry


def publish(entry: CacheEntry, state_dir: Path) -> None:
    """Write the payload so the GUI can pick it up."""
    write_atomic(state_dir / READINGS_FILE, encode_cache(entry))


def load(state_dir: Path) -> CacheEntry | None:
    """The published payload, or None when there is nothing usable. A missing
    file is normal (first boot, or the worker is not installed). A file that
    exists but cannot be decoded is logged and ignored: the panel then shows no
    data rather than numbers nobody can vouch for."""
    path = state_dir / READINGS_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Cannot read the readings cache %s: %s", path, exc)
        return None
    try:
        return decode_cache(text)
    except CacheFormatError as exc:
        logger.warning("Ignoring unusable readings cache %s: %s", path, exc)
        return None


class CacheReader:
    """The GUI's view of the cache.

    The GUI asks for the latest payload every frame, so this re-reads the file
    only when its modification time or size changed. A file that failed to
    decode is not retried until it changes again — re-parsing a known-bad file
    ten times a second would only fill the log.
    """

    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / READINGS_FILE
        self._stamp: tuple[int, int] | None = None
        self._entry: CacheEntry | None = None

    def latest(self, force: bool = False) -> CacheEntry | None:
        try:
            stat = self._path.stat()
        except OSError:
            # Gone (or unreadable): drop what was shown rather than keep
            # serving an entry whose backing file disappeared.
            self._stamp = None
            self._entry = None
            return None
        stamp = (stat.st_mtime_ns, stat.st_size)
        if not force and stamp == self._stamp:
            return self._entry
        self._stamp = stamp
        self._entry = load(self._path.parent)
        return self._entry


# -- worker status (diagnostics) ---------------------------------------------


@dataclass(frozen=True)
class RequestOutcome:
    """What the worker did with the GUI's last refresh request."""

    token: float
    result: str  # "polled" | "deferred" | "ignored"


@dataclass(frozen=True)
class WorkerStatus:
    updated_at: datetime
    state: str  # idle | fallback | budget | clock
    next_poll_at: datetime | None
    poll_interval: int
    last_ok_at: datetime | None
    last_source: str | None
    # Why the normal route is unusable ("" once it answers again), and what
    # went wrong on the cellular path ("" after a successful cellular read).
    # Kept apart because they fail independently: a panel can be on cellular
    # *because* of "dns" while the cellular side itself is fine.
    normal_error: str
    cellular_error: str
    month: str
    bytes_used: int
    budget_bytes: int
    request: RequestOutcome | None = None


def encode_status(status: WorkerStatus) -> str:
    doc: dict[str, Any] = {
        "schema": STATUS_SCHEMA,
        "updated_at": format_ts(status.updated_at),
        "state": status.state,
        "next_poll_at": format_ts(status.next_poll_at) if status.next_poll_at else None,
        "poll_interval": status.poll_interval,
        "last_ok_at": format_ts(status.last_ok_at) if status.last_ok_at else None,
        "last_source": status.last_source,
        "normal_error": status.normal_error,
        "cellular_error": status.cellular_error,
        "month": status.month,
        "bytes_used": status.bytes_used,
        "budget_bytes": status.budget_bytes,
    }
    if status.request is not None:
        doc["request"] = {"token": status.request.token, "result": status.request.result}
    return json.dumps(doc)


def decode_status(text: str) -> WorkerStatus:
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CacheFormatError(f"not JSON: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema") != STATUS_SCHEMA:
        raise CacheFormatError("not a cellular status document")
    try:
        request = doc.get("request")
        return WorkerStatus(
            updated_at=parse_ts(doc["updated_at"], "updated_at"),
            state=str(doc["state"]),
            next_poll_at=(
                parse_ts(doc["next_poll_at"], "next_poll_at")
                if doc.get("next_poll_at") else None
            ),
            poll_interval=int(doc["poll_interval"]),
            last_ok_at=(
                parse_ts(doc["last_ok_at"], "last_ok_at") if doc.get("last_ok_at") else None
            ),
            last_source=doc.get("last_source"),
            normal_error=str(doc.get("normal_error") or ""),
            cellular_error=str(doc.get("cellular_error") or ""),
            month=str(doc["month"]),
            bytes_used=int(doc["bytes_used"]),
            budget_bytes=int(doc["budget_bytes"]),
            request=(
                RequestOutcome(token=float(request["token"]), result=str(request["result"]))
                if isinstance(request, dict) else None
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, CacheFormatError):
            raise
        raise CacheFormatError(f"malformed status: {exc}") from exc


def write_status(status: WorkerStatus, state_dir: Path) -> None:
    write_atomic(state_dir / STATUS_FILE, encode_status(status))


def read_status(state_dir: Path) -> WorkerStatus | None:
    """The worker's last published status, or None (absent, or unusable)."""
    try:
        text = (state_dir / STATUS_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return decode_status(text)
    except CacheFormatError as exc:
        logger.warning("Ignoring unusable cellular status: %s", exc)
        return None


# -- GUI -> worker signals ---------------------------------------------------


def touch_awake(request_dir: Path) -> None:
    """Record "the panel is awake now". Called every few seconds while the
    panel is lit, so the worker can treat the file's age as a heartbeat: a
    crashed GUI stops refreshing it and the link goes idle on its own."""
    path = request_dir / AWAKE_FILE
    try:
        now = datetime.now(timezone.utc).timestamp()
        os.utime(path, (now, now))
    except OSError:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("")
        except OSError as exc:
            logger.debug("Cannot write the awake signal: %s", exc)


def clear_awake(request_dir: Path) -> None:
    """Record "the panel is dark" so the worker stops spending data at once
    instead of waiting for the heartbeat to age out."""
    try:
        (request_dir / AWAKE_FILE).unlink()
    except OSError:
        pass


def is_awake(request_dir: Path, now: float, timeout: float) -> bool:
    """Whether the panel is lit, per the GUI's heartbeat.

    Three states count as asleep: no heartbeat file (the GUI is dark, or is not
    publishing at all), a stale one (the GUI stopped, e.g. crashed or is
    between restarts), and one stamped in the future. The last matters at boot:
    a Pi without a synced clock can come up ahead of real time and then step
    backwards, leaving a heartbeat that would otherwise read "fresh" forever.
    """
    try:
        mtime = (request_dir / AWAKE_FILE).stat().st_mtime
    except OSError:
        return False
    age = now - mtime
    return 0 <= age <= timeout


def request_refresh(request_dir: Path, token: float) -> bool:
    """Ask the worker to re-read now. *token* identifies this request (the GUI
    uses the wall clock) so the worker can tell a new request from one it has
    already handled — the GUI owns this directory, so the worker cannot clear
    the file. Returns False if the signal could not be written."""
    try:
        write_atomic(request_dir / REFRESH_FILE, repr(float(token)))
        return True
    except OSError as exc:
        logger.warning("Cannot write the refresh signal: %s", exc)
        return False


def read_refresh_token(request_dir: Path) -> float | None:
    """The token of the GUI's latest refresh request, or None."""
    try:
        text = (request_dir / REFRESH_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return float(text)
    except ValueError:
        return None
