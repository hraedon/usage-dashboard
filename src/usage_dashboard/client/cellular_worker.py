"""The cellular readings worker (Plan 010).

An opted-in Pi keeps its normal link — Wi-Fi or Ethernet — as the preferred
path to the dashboard. This worker is the only thing on the unit allowed to use
the metered cellular fallback, and only to fetch one thing: the stored readings
snapshot. It runs as its own system user, separate from the GUI and the updater,
so the firewall around the modem interface can be written as "this one UID, to
this one address, on TCP 443".

How it decides, and what it never does:

* **Reachability is a real request.** A connected hotspot can have no working
  internet, so link state proves nothing. The worker asks for the readings over
  the normal route with bounded timeouts; only when that gets no HTTP response
  at all does it fall back. A server that *answers* — including with 401 or
  429 — is reachable: those are surfaced in the status file and never retried
  over cellular, where they would cost data and cannot succeed anyway.
* **The cellular route is selected only for the few seconds of a cellular
  read.** The privileged helper (``usage_dashboard.deploy.cellular_link``)
  installs a policy route for this worker's UID, the read happens, and the
  route is removed again. That is what keeps the worker's own "is the normal
  route back?" question honest: asked while the route were held, it would be
  answered over cellular and report a recovery that did not happen.
* **Data is spent only for a lit panel.** The GUI publishes an "awake"
  heartbeat; while it is dark, or absent, the worker keeps probing the normal
  route (free) but never touches the modem.
* **A refusal leaves nothing behind.** If the helper refuses part-way — a
  verification that did not pass, a command that failed, its own deadline —
  the worker asks for ``down`` before doing anything else. It takes a failed
  ``route`` to mean "no link", and its next act is to ask whether the normal
  route has recovered; a routing rule left behind would send that very
  question over cellular, uncounted, and report a recovery that did not
  happen.
* **A refresh tap may bring a read forward, not past a refusal.** Taps are
  subject to the minimum gap between cellular reads and to whatever wait
  follows a read that failed or was refused (the back-off ladders, and the
  rule that a 401 or 429 is not retried over the metered link).
* **Everything crosses one budget.** Bytes are counted from the modem
  interface's own packet counters and compared against a per-month cap the
  operator must set. Over the cap, the link is released and stays off until the
  next month.

The GUI never talks to this process. Results are published to a local cache
(:mod:`usage_dashboard.client.readings_cache`) that the GUI reads without any
network privilege, and the GUI's only signals back are two files it owns.

This module holds the decision logic and is unit-tested end to end against
injected fakes: a fetcher, a link, a clock, and the interface counters. The
real implementations of each are small classes at the bottom.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from usage_dashboard.client import readings_cache as cache

logger = logging.getLogger(__name__)

# sysexits.h EX_CONFIG. The unit's RestartPreventExitStatus names it: a missing
# or contradictory setting is fixed by an operator, not by restarting.
EXIT_CONFIG = 78

READINGS_PATH = "/api/v1/readings"

# The server answers these from its own state. Retrying them over a metered
# link cannot change the answer, so they back off to the maximum delay.
_NO_RETRY_STATUSES = frozenset({401, 403, 429})

# Consecutive unanswered probes on the normal route before the modem is
# touched. One is too eager: a single lost packet would cost an LTE attach.
FAILURES_TO_FALL_BACK = 2

# How far behind the last proven-good time the wall clock may be and still be
# taken for sound. A Pi has no real-time clock: at boot its clock is whatever
# its last time synchronisation left behind, so it runs stale by exactly as
# long as the unit was powered off — a week, on the first unit with the HAT
# fitted — until NTP catches up, which needs the normal route. That is the
# ordinary condition of the fallback scenario, and HTTPS validated fine there.
# Only a clock far behind that (never synchronised, or reset) is called unset.
CLOCK_SKEW_TOLERANCE_SECONDS = 3600

_MAX_CANDIDATES = 3
_TICK_SECONDS = 0.5
_INTERFACE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,14}$")
_USAGE_SCHEMA = "usage-dashboard-cellular-usage-v1"

OK = "ok"
HTTP = "http"
TRANSPORT = "transport"

# The names live with the status-file contract (readings_cache).
STATE_IDLE = cache.STATE_IDLE
STATE_FALLBACK = cache.STATE_FALLBACK
STATE_BUDGET = cache.STATE_BUDGET
STATE_CLOCK = cache.STATE_CLOCK


# -- outcomes ------------------------------------------------------------------


@dataclass(frozen=True)
class FetchOutcome:
    """The result of one read attempt against one address.

    The distinction that drives failover is ``reachable``: True whenever the
    server produced an HTTP status, whatever it was. Only a transport failure
    — no answer at all — means "this route does not reach the dashboard".
    """

    kind: str
    payload: list[dict[str, Any]] | None = None
    status: int | None = None
    reason: str = ""

    @property
    def reachable(self) -> bool:
        return self.kind != TRANSPORT

    @property
    def ok(self) -> bool:
        return self.kind == OK

    def label(self) -> str:
        """Short, stable description for the status file and the log."""
        if self.ok:
            return ""
        if self.kind == TRANSPORT:
            return self.reason or "transport"
        if self.status is None:
            return "http"
        return f"http_{self.status}" if not self.reason else f"{self.reason}_{self.status}"


@dataclass(frozen=True)
class LinkResult:
    ok: bool
    reason: str = ""


class Fetcher(Protocol):
    """Reads the readings snapshot from one literal address."""

    def normal_candidates(self) -> list[str]: ...

    def read(self, ip: str, *, cellular: bool) -> FetchOutcome: ...

    def close(self) -> None: ...


class Link(Protocol):
    """The privileged helper. ``route`` brings the link up behind the firewall
    and selects the cellular route for this worker; ``unroute`` withdraws the
    selection; ``down`` also takes the link down."""

    def __call__(self, verb: str) -> LinkResult: ...


class Signals(Protocol):
    """What the worker learns from the rest of the unit."""

    def awake(self) -> bool: ...

    def take_refresh(self) -> float | None: ...

    def iface_bytes(self) -> tuple[int, int] | None: ...

    def unsynced_clock(self) -> bool: ...


# -- configuration -------------------------------------------------------------


class ConfigError(ValueError):
    """A setting is missing or contradictory. Fatal without retries."""


def _require(env: Mapping[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def _number(
    env: Mapping[str, str], name: str, default: float, minimum: float, *, integer: bool = False,
) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw) if integer else float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum:g}, got {value:g}")
    return value


@dataclass(frozen=True)
class WorkerConfig:
    host: str
    port: int
    path: str
    display_key: str
    destinations: tuple[str, ...]
    # The TCP port on the pinned destinations. 443 for the dashboard's own
    # ingress; another port for a stable relay in front of it. Only the
    # cellular reads use it: the normal route always goes to SERVER_URL.
    dest_port: int
    interface: str
    helper: str
    state_dir: Path
    request_dir: Path
    probe_interval: float
    retry_delay: float
    poll_interval: float
    min_interval: float
    backoff_max: float
    awake_timeout: float
    connect_timeout: float
    read_timeout: float
    resolve_timeout: float
    budget_bytes: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> WorkerConfig:
        """Build the configuration, or raise :class:`ConfigError`.

        Nothing here has a "safe" default that would let the feature run by
        accident: the credential, the pinned destination, the modem interface
        and the data budget all have to be stated.
        """
        host, port, path = _server_url(_require(env, "SERVER_URL"))
        display_key = _require(env, "CELLULAR_DISPLAY_KEY")
        if not all(0x21 <= ord(ch) <= 0x7E for ch in display_key):
            raise ConfigError(
                "CELLULAR_DISPLAY_KEY must be printable ASCII with no spaces "
                "(the setting file cannot carry anything else safely)"
            )
        destinations = _destinations(_require(env, "CELLULAR_DEST"))
        dest_port = _number(env, "CELLULAR_DEST_PORT", 443.0, 1.0, integer=True)
        if not 1 <= dest_port <= 65535:
            raise ConfigError(f"CELLULAR_DEST_PORT must be a TCP port, got {dest_port:g}")
        interface = _require(env, "CELLULAR_IFACE")
        if not _INTERFACE_NAME.match(interface):
            raise ConfigError(
                "CELLULAR_IFACE must be a network interface name "
                "(letters, digits, '.', '_', '-'; at most 15 characters)"
            )
        budget_mb = _number(env, "CELLULAR_BUDGET_MB", 0.0, 0.0)
        if budget_mb <= 0:
            raise ConfigError(
                "CELLULAR_BUDGET_MB is required and must be greater than 0: the "
                "worker will not use a metered link without a monthly cap"
            )
        state_dir = Path(env.get("CELLULAR_STATE_DIR") or "/var/lib/usage-dashboard-cellular")
        request_dir = Path(env.get("CELLULAR_REQUEST_DIR") or state_dir / cache.REQUEST_DIR)

        probe_interval = _number(env, "CELLULAR_PROBE_INTERVAL", 120.0, 10.0)
        retry_delay = _number(env, "CELLULAR_RETRY_DELAY", 20.0, 5.0)
        poll_interval = _number(env, "CELLULAR_POLL_INTERVAL", 600.0, 60.0)
        min_interval = _number(env, "CELLULAR_MIN_INTERVAL", 60.0, 30.0)
        backoff_max = _number(env, "CELLULAR_BACKOFF_MAX", 3600.0, 60.0)
        if min_interval > poll_interval:
            raise ConfigError("CELLULAR_MIN_INTERVAL cannot exceed CELLULAR_POLL_INTERVAL")
        if backoff_max < poll_interval:
            raise ConfigError("CELLULAR_BACKOFF_MAX cannot be less than CELLULAR_POLL_INTERVAL")

        return cls(
            host=host,
            port=port,
            path=path,
            display_key=display_key,
            destinations=destinations,
            dest_port=int(dest_port),
            interface=interface,
            helper=env.get("CELLULAR_HELPER") or "/usr/local/bin/usage-dashboard-cellular-link",
            state_dir=state_dir,
            request_dir=request_dir,
            probe_interval=probe_interval,
            retry_delay=retry_delay,
            poll_interval=poll_interval,
            min_interval=min_interval,
            backoff_max=backoff_max,
            awake_timeout=_number(env, "CELLULAR_AWAKE_TIMEOUT", 150.0, 30.0),
            connect_timeout=_number(env, "CELLULAR_CONNECT_TIMEOUT", 8.0, 1.0),
            read_timeout=_number(env, "CELLULAR_READ_TIMEOUT", 15.0, 1.0),
            resolve_timeout=_number(env, "CELLULAR_RESOLVE_TIMEOUT", 5.0, 1.0),
            # Carriers bill in decimal units, so the cap is too: 50 MB is
            # 50,000,000 bytes, not 50 MiB.
            budget_bytes=int(budget_mb * 1_000_000),
        )


def _server_url(url: str) -> tuple[str, int, str]:
    """Validate ``SERVER_URL`` into ``(host, port, base_path)``.

    Cellular mode is "the same GET over verified HTTPS", so anything else is
    refused here rather than discovered as a firewall drop on a metered link:
    the scheme must be https, and the port must be the one the firewall lets
    through. Credentials, a query or a fragment in the URL would be silently
    ignored by this worker, so they are refused as well.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ConfigError(f"SERVER_URL is not a valid URL: {url!r}") from exc
    if parts.scheme != "https":
        raise ConfigError("SERVER_URL must use https for the cellular fallback")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise ConfigError("SERVER_URL has no hostname")
    if port not in (None, 443):
        raise ConfigError(
            "SERVER_URL must use port 443 (a relay's port goes in CELLULAR_DEST_PORT)"
        )
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError("SERVER_URL may not carry credentials, a query or a fragment")
    base = parts.path.rstrip("/")
    return host, 443, base + READINGS_PATH


def _destinations(raw: str) -> tuple[str, ...]:
    """Parse the pinned destination list into canonical address literals.

    These are literals, never names: resolving a name would need DNS, and the
    link permits none. The dashboard hostname still goes out as SNI and Host,
    so certificate verification and virtual-host routing are untouched.
    """
    addresses: list[str] = []
    for token in re.split(r"[,\s]+", raw.strip()):
        if not token:
            continue
        try:
            addresses.append(str(ipaddress.ip_address(token)))
        except ValueError as exc:
            raise ConfigError(
                f"CELLULAR_DEST entries must be IP literals; {token!r} is not"
            ) from exc
    if not addresses:
        raise ConfigError("CELLULAR_DEST must list at least one address")
    return tuple(dict.fromkeys(addresses))


# -- decision loop -------------------------------------------------------------


class UsageLedger:
    """Bytes counted against this month's data budget.

    The source is the modem interface's own packet counters, read from sysfs,
    so it measures what actually crossed the link — handshakes, retransmits
    and keepalives included — rather than what the application thinks it sent.
    Counters only ever increase while an interface exists; if one goes down,
    the device was re-enumerated (a re-plugged HAT) and the new baseline is
    taken without inventing a delta.
    """

    def __init__(self, path: Path, budget_bytes: int, now: Callable[[], float]) -> None:
        self._path = path
        self._budget = budget_bytes
        self._now = now
        today = self._month(now())
        saved = self._load()
        if saved is not None and saved[0] >= today:
            # The same month — so this survives a restart mid-month, and a
            # counter from before the restart is still a valid baseline unless
            # the interface was recreated, which observe() detects.
            #
            # Or an earlier month than the ledger was written under: the clock
            # is behind, not the ledger ahead. At boot a Pi without a
            # real-time clock reads its last synchronisation, so on the first
            # of a month (or after any long power-off) "today" is simply
            # wrong. Keep the ledger and keep counting in its month; starting
            # afresh here would hand out the whole budget again.
            self.month, self.bytes, self._last = saved
        else:
            self.month, self.bytes, self._last = today, 0, None

    @staticmethod
    def _month(ts: float) -> str:
        # Local calendar month: the unit's own timezone, which is how a bill
        # is read.
        return datetime.fromtimestamp(ts).strftime("%Y-%m")

    def _rollover(self) -> None:
        # Forward only. The month going backwards is the clock, not the bill.
        month = self._month(self._now())
        if month > self.month:
            self.month = month
            self.bytes = 0

    def _load(self) -> tuple[str, int, tuple[int, int] | None] | None:
        try:
            doc = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(doc, dict) or doc.get("schema") != _USAGE_SCHEMA:
            return None
        last = doc.get("last_counter")
        valid_last = (
            isinstance(last, list) and len(last) == 2 and all(isinstance(v, int) for v in last)
        )
        try:
            month = str(doc["month"])
            count = int(doc["bytes"])
        except (KeyError, TypeError, ValueError):
            return None
        baseline: tuple[int, int] | None = (last[0], last[1]) if valid_last else None  # type: ignore[index]
        return month, count, baseline

    def observe(self, counter: tuple[int, int] | None) -> None:
        """Fold a counter reading into this month's total."""
        self._rollover()
        if counter is not None:
            last = self._last
            if last is not None and counter[0] >= last[0] and counter[1] >= last[1]:
                self.bytes += (counter[0] - last[0]) + (counter[1] - last[1])
            self._last = counter
        try:
            cache.write_atomic(
                self._path,
                json.dumps(
                    {
                        "schema": _USAGE_SCHEMA,
                        "month": self.month,
                        "bytes": self.bytes,
                        "last_counter": list(self._last) if self._last else None,
                    }
                ),
            )
        except OSError as exc:
            # The budget still holds in memory for this run; only its
            # survival across a restart is lost.
            logger.warning("Cannot persist the data-usage ledger: %s", exc)

    def exhausted(self) -> bool:
        self._rollover()
        return self.bytes >= self._budget


class ReadingsWorker:
    """The failover state machine. See the module docstring for the contract."""

    def __init__(
        self,
        cfg: WorkerConfig,
        *,
        fetch: Fetcher,
        link: Link,
        signals: Signals,
        state_dir: Path | None = None,
        on_publish: Callable[[datetime], None] | None = None,
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], bool] | None = None,
    ) -> None:
        self._cfg = cfg
        self._fetch = fetch
        self._link_call = link
        self._signals = signals
        self._state_dir = state_dir if state_dir is not None else cfg.state_dir
        self._on_publish = on_publish
        self._now = now
        self._monotonic = monotonic
        self._stop = threading.Event()
        self._sleep = sleep if sleep is not None else self._stop.wait

        self._usage = UsageLedger(self._state_dir / cache.USAGE_FILE, cfg.budget_bytes, now)
        # Normal route is presumed healthy until it fails to answer.
        self._fallback = False
        self._failures = 0
        self._link_up = False
        # True only after a cellular read succeeded and until the next
        # attempt: a refresh tap may bring a read forward only while this holds.
        self._cellular_ready = False
        self._wait = cfg.poll_interval      # after a failed cellular read
        self._link_wait = cfg.retry_delay   # after a failed link attempt
        self._last_cellular: float | None = None
        self._last_ok: datetime | None = None
        self._last_source: str | None = None
        self._normal_error = ""     # why the normal route is unusable
        self._cellular_error = ""   # what went wrong on the cellular path
        self._state = STATE_IDLE
        self._request: cache.RequestOutcome | None = None
        self._next_poll_at: datetime | None = None

    def stop(self) -> None:
        self._stop.set()

    # -- one cycle ------------------------------------------------------------

    def cycle(self) -> float:
        """Run one decision pass; return how long to wait before the next."""
        now = self._now()
        outcome = self._read_normal()

        if outcome.reachable:
            self._failures = 0
            if self._fallback:
                logger.info("Normal route reaches the dashboard again; leaving cellular fallback")
                self._fallback = False
                self._cellular_error = ""
                self._cellular_ready = False
            # Also retried here on later cycles if a previous attempt to take
            # the link down failed: a link nobody is supervising is not left up.
            if self._link_up:
                self._release()
            self._normal_error = outcome.label()
            self._state = STATE_IDLE
            if outcome.ok:
                self._publish(outcome.payload or [], cache.SOURCE_NETWORK, now)
            return self._cfg.probe_interval

        self._failures += 1
        if not self._fallback:
            if self._failures < FAILURES_TO_FALL_BACK:
                self._normal_error = outcome.label()
                self._state = STATE_IDLE
                return self._cfg.retry_delay
            logger.info(
                "Normal route has not answered %d probes (%s); entering cellular fallback",
                self._failures,
                outcome.label(),
            )
            self._fallback = True
            self._wait = self._cfg.poll_interval
            self._last_cellular = None
            self._cellular_ready = False

        self._normal_error = outcome.label()
        if not self._signals.awake():
            # Nobody is looking. Keep the normal-route probes going — they are
            # free, and they are how recovery is noticed — but the modem stays
            # off rather than idling on the carrier's network all night.
            self._release()
            self._state = STATE_FALLBACK
            return self._cfg.probe_interval
        if self._usage.exhausted():
            self._release()
            self._state = STATE_BUDGET
            return self._cfg.poll_interval
        if self._signals.unsynced_clock():
            # Without a trustworthy clock HTTPS cannot validate, so every
            # attempt would spend data on a handshake destined to fail. This is
            # the cold-boot case: no working link means no time sync either.
            self._release()
            self._state = STATE_CLOCK
            return self._cfg.poll_interval
        return self._cellular_cycle()

    def _cellular_cycle(self) -> float:
        engaged = self._link("route")
        if not engaged.ok:
            self._cellular_error = f"link_{engaged.reason}"
            self._cellular_ready = False
            self._state = STATE_FALLBACK
            self._settle()
            # The modem may simply be absent: retry soon, then less and less
            # often. A link that cannot come up costs no data, so this ladder
            # starts far shorter than the one for failed reads.
            delay = self._link_wait
            self._link_wait = min(self._link_wait * 2, self._cfg.backoff_max)
            return delay
        self._link_up = True
        self._link_wait = self._cfg.retry_delay
        # A baseline right after the link comes up. Any later reading then
        # covers the idle time in between as well as the read itself — and if
        # that idle time alone used up the budget, the read is not started.
        self._usage.observe(self._signals.iface_bytes())
        if self._usage.exhausted():
            return self._stop_for_budget()

        self._cellular_ready = False
        self._last_cellular = self._monotonic()
        try:
            outcome = self._read_cellular()
        finally:
            withdrawn = self._link("unroute")
            self._usage.observe(self._signals.iface_bytes())
            if not withdrawn.ok:
                # The selection could not be withdrawn. Taking the link down
                # is the only way to be sure nothing else can use it.
                logger.error("Could not withdraw the cellular route (%s); taking the link down",
                             withdrawn.reason)
                self._release()
        if self._usage.exhausted():
            return self._stop_for_budget()

        self._state = STATE_FALLBACK
        self._cellular_error = outcome.label()
        if outcome.ok:
            self._publish(outcome.payload or [], cache.SOURCE_CELLULAR, self._now())
            self._wait = self._cfg.poll_interval
            self._cellular_ready = True
        elif outcome.reachable and outcome.status in _NO_RETRY_STATUSES:
            # The link works; the request itself is wrong or unwelcome. Say so
            # and stay quiet for as long as the backoff allows.
            logger.warning("Cellular read refused by the server (%s); not retrying soon",
                           outcome.label())
            self._wait = self._cfg.backoff_max
        else:
            self._wait = min(self._wait * 2, self._cfg.backoff_max)
        return self._wait

    def _stop_for_budget(self) -> float:
        self._release()
        self._state = STATE_BUDGET
        return self._cfg.poll_interval

    # -- pieces of a cycle -----------------------------------------------------

    def _read_normal(self) -> FetchOutcome:
        candidates = self._fetch.normal_candidates()
        if not candidates:
            return FetchOutcome(TRANSPORT, reason="dns")
        return self._first_answer(candidates, cellular=False)

    def _read_cellular(self) -> FetchOutcome:
        return self._first_answer(self._cfg.destinations, cellular=True)

    def _first_answer(self, addresses: Sequence[str], *, cellular: bool) -> FetchOutcome:
        """Ask each address until one produces an HTTP status.

        A multi-address answer (IPv4 and IPv6, or several A records) is one
        destination as far as failover is concerned: it is unreachable only
        when none of them answers.
        """
        outcome = FetchOutcome(TRANSPORT, reason="connect")
        for ip in list(addresses)[:_MAX_CANDIDATES]:
            outcome = self._fetch.read(ip, cellular=cellular)
            if outcome.reachable:
                return outcome
        return outcome

    def _link(self, verb: str) -> LinkResult:
        """Call the privileged helper; never let it take the loop down."""
        try:
            return self._link_call(verb)
        except Exception as exc:  # the helper is a separate root process
            logger.exception("Cellular link helper raised during %s", verb)
            return LinkResult(False, f"helper_{type(exc).__name__}")

    def _settle(self) -> None:
        """After a failed ``route``, make sure none of it is left.

        The helper unwinds its own refusals, but the worker cannot tell a
        refusal from a helper that was killed on its deadline half-way through
        installing a route. ``down`` is idempotent, so asking costs nothing
        when there is nothing to undo; not asking risks the one state this
        worker must never be in — believing the link is down while its rule
        still carries the worker's traffic.

        The link-attempt back-off is deliberately left alone: a ``route`` that
        keeps failing must be retried less and less often, whether or not
        there was anything to undo afterwards.
        """
        if self._link("down").ok:
            self._link_up = False
        else:
            # Unknown: later cycles keep asking, as for any link believed up.
            self._link_up = True
        self._usage.observe(self._signals.iface_bytes())

    def _release(self) -> LinkResult:
        """Take the modem down if we believe it is up. Safe to call freely."""
        if not self._link_up:
            return LinkResult(True)
        result = self._link("down")
        if result.ok:
            self._link_up = False
            self._link_wait = self._cfg.retry_delay
        else:
            # Still believed up: the next cycle will try again.
            logger.error("Could not take the cellular link down: %s", result.reason)
        self._usage.observe(self._signals.iface_bytes())
        return result

    def _publish(self, payload: list[dict[str, Any]], source: str, now: float) -> None:
        cellular = source == cache.SOURCE_CELLULAR
        entry = cache.CacheEntry(
            written_at=datetime.fromtimestamp(now, timezone.utc).replace(tzinfo=None),
            source=source,
            poll_interval=int(self._cfg.poll_interval if cellular else self._cfg.probe_interval),
            raw=payload,
        )
        try:
            cache.publish(entry, self._state_dir)
        except OSError as exc:
            logger.warning("Cannot publish the readings cache: %s", exc)
            if cellular:
                self._cellular_error = "cache_write_failed"
            else:
                self._normal_error = "cache_write_failed"
            return
        self._last_ok = entry.written_at
        self._last_source = source
        if self._on_publish is not None:
            self._on_publish(entry.written_at)

    # -- status ---------------------------------------------------------------

    def status(self) -> cache.WorkerStatus:
        return cache.WorkerStatus(
            updated_at=datetime.fromtimestamp(self._now(), timezone.utc).replace(tzinfo=None),
            state=self._state,
            next_poll_at=self._next_poll_at,
            poll_interval=int(self._cfg.poll_interval),
            last_ok_at=self._last_ok,
            last_source=self._last_source,
            normal_error=self._normal_error,
            cellular_error=self._cellular_error,
            month=self._usage.month,
            bytes_used=self._usage.bytes,
            budget_bytes=self._cfg.budget_bytes,
            request=self._request,
        )

    def _write_status(self) -> None:
        try:
            cache.write_status(self.status(), self._state_dir)
        except OSError as exc:
            logger.warning("Cannot write the cellular status: %s", exc)

    # -- waiting --------------------------------------------------------------

    def _pause(self, seconds: float) -> str | None:
        """Wait up to *seconds*, returning early for a reason or None at the end.

        The wait is sliced so two things cut it short: the panel waking (the
        plan calls for an immediate read then) and the GUI's refresh tap. A tap
        the worker will not act on is declined here rather than ignored, so the
        GUI can say so (:meth:`_tap_allowed`). Signals are checked before each
        slice, not after, so a tap that arrived while the previous cycle was
        still running is answered without further delay.
        """
        deadline = self._monotonic() + max(0.0, seconds)
        awake = self._signals.awake()
        while not self._stop.is_set():
            token = self._signals.take_refresh()
            if token is not None:
                if self._fallback and not self._tap_allowed():
                    self._request = cache.RequestOutcome(token, "deferred")
                    self._write_status()
                else:
                    self._request = cache.RequestOutcome(token, "polled")
                    return "refresh"
            now_awake = self._signals.awake()
            if now_awake and not awake:
                return "wake"
            awake = now_awake
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return None
            if self._sleep(min(remaining, _TICK_SECONDS)):
                return "stop"
        return "stop"

    def _tap_allowed(self) -> bool:
        """Whether, in fallback, a refresh tap may cause a cellular read now.

        A tap may bring the next read forward but never past two limits. The
        first is the minimum gap between reads. The second is any wait left
        after a read that failed or was refused — the back-off ladders, and
        the hour a 401 or 429 buys. Those exist so that data is not spent on
        attempts that cannot succeed, and a tap is not a reason to set them
        aside; so a tap only counts while the last read succeeded.
        """
        if self._within_min_gap():
            return False
        return self._cellular_ready

    def _within_min_gap(self) -> bool:
        return (
            self._last_cellular is not None
            and self._monotonic() - self._last_cellular < self._cfg.min_interval
        )

    # -- lifetime -------------------------------------------------------------

    def run(self) -> None:
        """Run until :meth:`stop`. On the way in and on the way out the modem
        is taken down, so a crash can never leave a link up that nobody is
        supervising."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        # The deny policy first, in case the boot unit that loads it could
        # not (it only restricts traffic, so there is nothing to get wrong).
        self._link("firewall")
        # The predecessor (or this process, before a restart) may have died
        # with the link up. `down` is idempotent, so this is cheap when clean.
        self._link_up = True
        self._release()
        logger.info(
            "Cellular readings worker started: %s via %s, budget %d bytes/month",
            READINGS_PATH, self._cfg.interface, self._cfg.budget_bytes,
        )
        try:
            while not self._stop.is_set():
                try:
                    delay = self.cycle()
                except Exception:
                    # A bug in one pass must not stop the next one; the link is
                    # released because its state is now unknown.
                    logger.exception("Cellular cycle failed")
                    self._link_up = True
                    self._release()
                    delay = self._cfg.retry_delay
                self._next_poll_at = datetime.fromtimestamp(
                    self._now() + delay, timezone.utc
                ).replace(tzinfo=None)
                self._write_status()
                if self._pause(delay) == "stop":
                    break
        finally:
            self._release()
            self._next_poll_at = None
            self._write_status()
            self._fetch.close()
            logger.info("Cellular readings worker stopped")


# -- real building blocks --------------------------------------------------------


def _classify(exc: BaseException) -> str:
    """Name the class of a transport failure.

    The names end up in the status file and on the panel, so they say what to
    do: ``timeout`` and ``connect`` mean the route is not there; ``tls`` and
    ``tls_verify`` mean it is, but the handshake was refused — most often a
    clock that has not been set.
    """
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    seen: set[int] = set()
    cause: BaseException | None = exc
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, ssl.SSLCertVerificationError):
            return "tls_verify"
        if isinstance(cause, ssl.SSLError):
            return "tls"
        if isinstance(cause, socket.gaierror):
            return "dns"
        cause = cause.__cause__ if cause.__cause__ is not None else cause.__context__
    if isinstance(exc, httpx.ConnectError):
        return "connect"
    if isinstance(exc, httpx.ProtocolError):
        return "protocol"
    if isinstance(exc, httpx.DecodingError):
        return "decode"
    return "io"


def _url_host(ip: str) -> str:
    return f"[{ip}]" if ":" in ip else ip


def _sni_hostname(host: str) -> bytes:
    try:
        return host.encode("ascii")
    except UnicodeEncodeError:
        return host.encode("idna")


def _request(
    client: httpx.Client, cfg: WorkerConfig, ip: str, port: int | None = None
) -> FetchOutcome:
    """One GET against the literal *ip*, addressed to ``cfg.host``.

    Three settings here are the whole reason this function exists rather than
    calling the URL directly:

    * the connection goes to *ip* — pinned for cellular, resolved under a
      bounded timeout otherwise — so no name lookup happens on the metered
      path;
    * ``sni_hostname`` and ``Host`` carry the real dashboard hostname, so the
      certificate is verified against the name the operator trusts and the
      ingress routes by its virtual host. An address override that also
      changed the verified name would defeat both;
    * redirects are not followed. A redirect on this endpoint means the
      destination is not the dashboard; chasing it would spend data on an
      address the firewall has not approved — and be dropped anyway.
    """
    url = f"https://{_url_host(ip)}:{port or cfg.port}{cfg.path}"
    try:
        response = client.get(
            url,
            headers={
                "Host": cfg.host,
                "Authorization": f"Bearer {cfg.display_key}",
                "Accept": "application/json",
            },
            extensions={"sni_hostname": _sni_hostname(cfg.host)},
        )
    except httpx.HTTPError as exc:
        return FetchOutcome(TRANSPORT, reason=_classify(exc))
    if response.status_code != 200:
        return FetchOutcome(HTTP, status=response.status_code)
    try:
        payload: Any = response.json()
    except ValueError:
        return FetchOutcome(HTTP, status=200, reason="payload")
    if not is_payload(payload):
        return FetchOutcome(HTTP, status=200, reason="payload")
    return FetchOutcome(OK, payload=payload, status=200)


def is_payload(value: Any) -> bool:
    """Whether *value* is shaped like the readings snapshot: a list of objects
    each naming a provider. The GUI decodes each entry strictly; this only
    guards against publishing something that is obviously not the snapshot."""
    return isinstance(value, list) and all(
        isinstance(item, dict) and isinstance(item.get("provider"), str) for item in value
    )


def _system_lookup(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class BoundedResolver:
    """Name resolution that cannot block the worker.

    ``getaddrinfo`` has no timeout of its own, and on a captive or half-broken
    hotspot it can hang for tens of seconds per attempt — longer than every
    other bound in the worker put together. The lookup therefore runs in a
    helper thread and the caller waits on it for a fixed time.

    At most one lookup is ever in flight: a lookup that outlives its timeout
    keeps holding the lock until the resolver returns, and later calls fail
    fast instead of piling up stuck threads. While it is stuck the last good
    answer is reused, which also covers the common case of a flaky resolver
    in front of a working route. This class is written for a single caller
    thread (the worker loop); ``locked`` followed by ``acquire`` is not atomic.
    """

    def __init__(
        self, timeout: float, *, lookup: Callable[[str, int], list[str]] | None = None
    ) -> None:
        self._timeout = timeout
        self._lookup = lookup if lookup is not None else _system_lookup
        self._lock = threading.Lock()
        self._last: list[str] | None = None

    def _run(self, host: str, port: int, box: dict[str, Any]) -> None:
        try:
            box["addresses"] = self._lookup(host, port)
        except OSError as exc:
            box["error"] = exc
        finally:
            self._lock.release()

    def resolve(self, host: str, port: int) -> list[str] | None:
        """Addresses for *host*, a cached answer if the lookup cannot finish,
        or None when there is nothing to return."""
        try:
            return [str(ipaddress.ip_address(host))]
        except ValueError:
            pass  # a name; resolve it
        if self._lock.locked():
            return list(self._last) if self._last else None
        self._lock.acquire()
        box: dict[str, Any] = {}
        thread = threading.Thread(
            target=self._run, args=(host, port, box), daemon=True, name="cellular-resolver"
        )
        thread.start()
        thread.join(self._timeout)
        if thread.is_alive():
            logger.debug("Name lookup for %s did not finish in %.0fs", host, self._timeout)
            return list(self._last) if self._last else None
        if "error" in box:
            logger.info("Name lookup for %s failed: %s", host, box["error"])
            return list(self._last) if self._last else None
        addresses = list(dict.fromkeys(box.get("addresses") or []))
        if addresses:
            self._last = addresses
            return list(addresses)
        return list(self._last) if self._last else None


def _new_client(cfg: WorkerConfig) -> httpx.Client:
    # trust_env=False: no proxy inherited from the unit's environment. A proxy
    # would be a second destination the link never approved. follow_redirects
    # is off for the reason given in _request().
    return httpx.Client(
        trust_env=False,
        follow_redirects=False,
        http2=False,
        timeout=httpx.Timeout(
            connect=cfg.connect_timeout,
            read=cfg.read_timeout,
            write=cfg.read_timeout,
            pool=1.0,
        ),
    )


class HttpFetcher:
    """The real fetcher. One connection pool per route, deliberately: a
    keep-alive connection opened over Wi-Fi must never be reused for a cellular
    request to the same address, or a read would leave by the wrong link and
    report the wrong route's health."""

    def __init__(
        self,
        cfg: WorkerConfig,
        *,
        resolve: Callable[[str, int], list[str] | None] | None = None,
        client_factory: Callable[[], httpx.Client] | None = None,
    ) -> None:
        self._cfg = cfg
        factory = client_factory if client_factory is not None else (lambda: _new_client(cfg))
        self._normal = factory()
        self._cellular = factory()
        if resolve is None:
            resolve = BoundedResolver(cfg.resolve_timeout).resolve
        self._resolve = resolve

    def normal_candidates(self) -> list[str]:
        found = self._resolve(self._cfg.host, self._cfg.port)
        return list(found) if found else []

    def read(self, ip: str, *, cellular: bool) -> FetchOutcome:
        if cellular:
            return _request(self._cellular, self._cfg, ip, self._cfg.dest_port)
        return _request(self._normal, self._cfg, ip)

    def close(self) -> None:
        self._normal.close()
        self._cellular.close()


class SudoLink:
    """Calls the root-owned link helper through sudo.

    The helper takes exactly one word — the verb — and the unit's sudoers rule
    grants exactly those three command lines, so nothing the worker could say
    reaches a shell. stderr's first line is the helper's reason for failing and
    is kept short: it is copied into a file the panel displays.
    """

    # `route` may wait a full minute for the link, then verify; the helper
    # keeps its own deadline 50 s below this (see deploy.cellular_link).
    _TIMEOUTS = {"firewall": 30.0, "route": 150.0, "unroute": 30.0, "down": 60.0}

    def __init__(
        self,
        helper: str,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._helper = helper
        self._runner = runner

    def __call__(self, verb: str) -> LinkResult:
        if verb not in self._TIMEOUTS:
            return LinkResult(False, "verb")
        command = ["sudo", "-n", self._helper, verb]
        try:
            proc = self._runner(
                command, capture_output=True, text=True, check=False,
                timeout=self._TIMEOUTS[verb],
            )
        except subprocess.TimeoutExpired:
            return LinkResult(False, "timeout")
        except OSError as exc:
            return LinkResult(False, f"helper_{exc.__class__.__name__}")
        if proc.returncode == 0:
            return LinkResult(True)
        for stream in (proc.stderr, proc.stdout):
            line = next((ln.strip() for ln in stream.splitlines() if ln.strip()), "")
            if line:
                return LinkResult(False, line[:80])
        return LinkResult(False, f"exit_{proc.returncode}")


class FileSignals:
    """What the GUI and the kernel tell the worker, read from the filesystem."""

    def __init__(
        self,
        state_dir: Path,
        request_dir: Path,
        interface: str,
        awake_timeout: float,
        *,
        sysfs_net: Path = Path("/sys/class/net"),
    ) -> None:
        self._request_dir = request_dir
        self._statistics = sysfs_net / interface / "statistics"
        self._awake_timeout = awake_timeout
        self._now = time.time
        self._seen_refresh = cache.read_refresh_token(request_dir)
        # The newest instant at which this unit provably had a correct clock:
        # a published payload required a verified TLS handshake. If the wall
        # clock is ever earlier than that, it has not been set since boot.
        published = cache.load(state_dir)
        self._clock_floor: datetime | None = published.written_at if published else None

    def note_published(self, written_at: datetime) -> None:
        self._clock_floor = written_at

    def awake(self) -> bool:
        return cache.is_awake(self._request_dir, self._now(), self._awake_timeout)

    def take_refresh(self) -> float | None:
        token = cache.read_refresh_token(self._request_dir)
        if token is None or token <= (self._seen_refresh or float("-inf")):
            return None
        self._seen_refresh = token
        return token

    def iface_bytes(self) -> tuple[int, int] | None:
        try:
            return (
                int((self._statistics / "rx_bytes").read_text().strip()),
                int((self._statistics / "tx_bytes").read_text().strip()),
            )
        except (OSError, ValueError):
            return None

    def unsynced_clock(self) -> bool:
        if self._clock_floor is None:
            return False
        # The floor is naive UTC (the readings convention); timestamp() would
        # read a naive datetime as local time, off by the unit's UTC offset.
        floor = self._clock_floor.replace(tzinfo=timezone.utc).timestamp()
        return self._now() < floor - CLOCK_SKEW_TOLERANCE_SECONDS


def build(cfg: WorkerConfig) -> ReadingsWorker:
    """Assemble the worker from its real parts."""
    signals = FileSignals(cfg.state_dir, cfg.request_dir, cfg.interface, cfg.awake_timeout)
    return ReadingsWorker(
        cfg,
        fetch=HttpFetcher(cfg),
        link=SudoLink(cfg.helper),
        signals=signals,
        on_publish=signals.note_published,
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        cfg = WorkerConfig.from_env(os.environ)
    except ConfigError as exc:
        logger.error("Cellular readings worker is not configured: %s", exc)
        return EXIT_CONFIG
    worker = build(cfg)

    def _handle_sigterm(signum: int, frame: Any) -> None:
        logger.info("Received SIGTERM, shutting down")
        worker.stop()

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    worker.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
