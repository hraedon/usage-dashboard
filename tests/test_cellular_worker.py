"""The cellular readings worker's decisions (Plan 010).

None of this can be exercised against a real modem from a test runner, so the
state machine is driven end to end against fakes for everything it touches:
what each route answers, what the privileged link helper does, the modem
interface's packet counters, whether the panel is lit, and the clock. What
these tests pin is the plan's behaviour list:

* fail over only when the normal route stops answering — never on an HTTP
  error, and never after a single lost probe;
* select the cellular route only around a cellular read, so the worker's own
  recovery probe cannot be answered over cellular by mistake;
* read cellular only for a lit panel, and stop at the data budget;
* leave the modem down whenever nobody is supervising it.
"""
from __future__ import annotations

import json
import logging
import ssl
import subprocess
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from usage_dashboard.client import cellular_worker as cw
from usage_dashboard.client import readings_cache as cache
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus

PIN = "192.0.2.10"          # TEST-NET-1: the pinned dashboard address
PIN6 = "2001:db8::10"       # documentation range
NORMAL_IP = "198.51.100.7"  # TEST-NET-2: what the system resolver answers
HOST = "dash.example"
NOW = 1_800_000_000.0       # a fixed wall clock (2027-01-15ish), seconds
BASE_MONO = 5_000.0


def _payload(*providers: Provider) -> list[dict[str, Any]]:
    fetched = datetime(2027, 1, 15, 12, 0, 0)
    providers = providers or (Provider.CLAUDE, Provider.ZAI)
    return [
        Reading(p, ReadingStatus.CURRENT, 40.0, fetched, 55.0, fetched, fetched, False).to_dict()
        for p in providers
    ]


def ok(*providers: Provider) -> cw.FetchOutcome:
    return cw.FetchOutcome(cw.OK, payload=_payload(*providers), status=200)


def http(status: int) -> cw.FetchOutcome:
    return cw.FetchOutcome(cw.HTTP, status=status)


DEAD = cw.FetchOutcome(cw.TRANSPORT, reason="timeout")


def _base_cfg(tmp_path: Path) -> cw.WorkerConfig:
    return cw.WorkerConfig(
        host=HOST, port=443, path=cw.READINGS_PATH, display_key="display-test",
        destinations=(PIN,), dest_port=443, interface="cell0",
        helper="/usr/local/bin/usage-dashboard-cellular-link",
        state_dir=tmp_path, request_dir=tmp_path / "request",
        probe_interval=120.0, retry_delay=20.0, poll_interval=600.0, min_interval=60.0,
        backoff_max=3600.0, awake_timeout=150.0, connect_timeout=8.0, read_timeout=15.0,
        resolve_timeout=5.0, budget_bytes=50_000_000,
    )


class FakeFetcher:
    def __init__(self) -> None:
        self.normal: cw.FetchOutcome = ok()
        self.cellular: cw.FetchOutcome = ok()
        self.candidates: list[str] | None = [NORMAL_IP]
        self.events: list[tuple[str, ...]] = []
        self.closed = False

    def normal_candidates(self) -> list[str]:
        return list(self.candidates or [])

    def read(self, ip: str, *, cellular: bool) -> cw.FetchOutcome:
        self.events.append(("cellular" if cellular else "normal", ip))
        return self.cellular if cellular else self.normal

    def close(self) -> None:
        self.closed = True


class FakeLink:
    def __init__(self, events: list[tuple[str, ...]]) -> None:
        self.events = events
        self.results: dict[str, cw.LinkResult] = {}

    def __call__(self, verb: str) -> cw.LinkResult:
        self.events.append(("link", verb))
        result = self.results.get(verb, cw.LinkResult(True))
        if isinstance(result, BaseException):
            raise result
        return result

    @property
    def verbs(self) -> list[str]:
        return [event[1] for event in self.events if event[0] == "link"]


class FakeSignals:
    def __init__(self) -> None:
        self.is_awake = True
        self.refresh_tokens: list[float] = []
        self.counter: tuple[int, int] | None = None
        self.unsynced = False

    def awake(self) -> bool:
        return self.is_awake

    def take_refresh(self) -> float | None:
        return self.refresh_tokens.pop(0) if self.refresh_tokens else None

    def iface_bytes(self) -> tuple[int, int] | None:
        return self.counter

    def unsynced_clock(self) -> bool:
        return self.unsynced


class FakeClock:
    def __init__(self) -> None:
        self.wall = NOW
        self.mono = BASE_MONO
        self.slept: list[float] = []

    def now(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds

    def sleep(self, seconds: float) -> bool:
        self.slept.append(seconds)
        self.advance(seconds)
        return False


class Harness:
    def __init__(self, tmp_path: Path, **overrides: Any) -> None:
        self.tmp = tmp_path
        self.cfg = replace(_base_cfg(tmp_path), **overrides)
        self.fetch = FakeFetcher()
        self.link = FakeLink(self.fetch.events)
        self.signals = FakeSignals()
        self.clock = FakeClock()
        self.worker = cw.ReadingsWorker(
            self.cfg, fetch=self.fetch, link=self.link, signals=self.signals,
            state_dir=tmp_path, now=self.clock.now, monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
        )
        self.read_at = BASE_MONO

    def step(self) -> float:
        delay = self.worker.cycle()
        self.clock.advance(delay)
        return delay

    @property
    def cached(self) -> cache.CacheEntry | None:
        return cache.load(self.tmp)

    @property
    def status(self) -> cache.WorkerStatus | None:
        return self.worker.status()

    def cellular_reads(self) -> list[str]:
        return [ip for kind, ip in self.fetch.events if kind == "cellular"]


def _enter_fallback(h: Harness) -> None:
    """Drive a healthy unit until the normal route is dead and fallback is on.

    Leaves the unit having just completed the engaging cycle — route, cellular
    read, unroute — with the event log holding exactly that cycle, and
    ``h.read_at`` the monotonic instant of that read."""
    h.step()
    h.fetch.normal = DEAD
    h.step()
    h.fetch.events.clear()
    delay = h.worker.cycle()
    h.read_at = h.clock.mono
    h.clock.advance(delay)
    status = h.worker.status()
    assert status.state == cw.STATE_FALLBACK and status.normal_error == "timeout"


# -- configuration -----------------------------------------------------------


def _env() -> dict[str, str]:
    return {
        "SERVER_URL": "https://dash.example",
        "CELLULAR_DISPLAY_KEY": "display-test",
        "CELLULAR_DEST": PIN,
        "CELLULAR_IFACE": "cell0",
        "CELLULAR_BUDGET_MB": "50",
    }


class TestConfig:
    def test_defaults(self) -> None:
        cfg = cw.WorkerConfig.from_env(_env())
        assert cfg.host == HOST and cfg.port == 443 and cfg.path == cw.READINGS_PATH
        assert cfg.destinations == (PIN,)
        assert cfg.budget_bytes == 50_000_000  # decimal, as carriers bill
        assert (cfg.probe_interval, cfg.retry_delay, cfg.poll_interval) == (120.0, 20.0, 600.0)
        assert (cfg.min_interval, cfg.backoff_max) == (60.0, 3600.0)
        assert cfg.helper == "/usr/local/bin/usage-dashboard-cellular-link"
        assert cfg.request_dir == cfg.state_dir / "request"

    @pytest.mark.parametrize(
        "name", ["SERVER_URL", "CELLULAR_DISPLAY_KEY", "CELLULAR_DEST", "CELLULAR_IFACE",
                 "CELLULAR_BUDGET_MB"]
    )
    def test_every_provisioning_fact_must_be_stated(self, name: str) -> None:
        # None of these has a default that would let the feature run by accident.
        env = _env()
        del env[name]
        with pytest.raises(cw.ConfigError, match=name):
            cw.WorkerConfig.from_env(env)

    @pytest.mark.parametrize(
        "url",
        [
            "http://dash.example",
            "http://dash.example:8080",
            "https://dash.example:8443",
            "https://user:pw@dash.example",
            "https://dash.example?token=x",
            "https://dash.example#frag",
            "https://",
            "not a url",
        ],
    )
    def test_only_plain_https_on_443_is_accepted(self, url: str) -> None:
        env = _env() | {"SERVER_URL": url}
        with pytest.raises(cw.ConfigError, match="SERVER_URL"):
            cw.WorkerConfig.from_env(env)

    def test_a_base_path_and_trailing_slash_are_tolerated(self) -> None:
        env = _env() | {"SERVER_URL": "https://dash.example/sub/"}
        assert cw.WorkerConfig.from_env(env).path == "/sub" + cw.READINGS_PATH

    def test_destinations_are_literals_and_canonical(self) -> None:
        env = _env() | {"CELLULAR_DEST": f"{PIN}, 2001:DB8::0001,{PIN}"}
        assert cw.WorkerConfig.from_env(env).destinations == (PIN, "2001:db8::1")

    def test_dest_port_defaults_to_443_and_accepts_a_relay_port(self) -> None:
        assert cw.WorkerConfig.from_env(_env()).dest_port == 443
        cfg = cw.WorkerConfig.from_env(_env() | {"CELLULAR_DEST_PORT": "8443"})
        # The relay's port is for the pinned address only: SERVER_URL stays 443.
        assert cfg.dest_port == 8443 and cfg.port == 443

    @pytest.mark.parametrize("port", ["0", "65536", "-1", "443.5", "https"])
    def test_dest_port_must_be_a_tcp_port(self, port: str) -> None:
        env = _env() | {"CELLULAR_DEST_PORT": port}
        with pytest.raises(cw.ConfigError, match="CELLULAR_DEST_PORT"):
            cw.WorkerConfig.from_env(env)

    @pytest.mark.parametrize("dest", ["dash.example", "192.0.2.10/32", " ", "10.0.0.256"])
    def test_a_name_or_malformed_address_is_refused(self, dest: str) -> None:
        # Resolving a name on the metered path would need DNS, which the link
        # does not permit; the hostname travels as SNI and Host instead.
        env = _env() | {"CELLULAR_DEST": dest}
        with pytest.raises(cw.ConfigError, match="CELLULAR_DEST"):
            cw.WorkerConfig.from_env(env)

    @pytest.mark.parametrize("iface", ["", "cell 0", "cell0;reboot", "x" * 16, "../eth0"])
    def test_interface_must_be_a_plain_name(self, iface: str) -> None:
        env = _env() | {"CELLULAR_IFACE": iface}
        with pytest.raises(cw.ConfigError, match="CELLULAR_IFACE"):
            cw.WorkerConfig.from_env(env)

    @pytest.mark.parametrize("budget", ["0", "-5", "abc"])
    def test_budget_must_be_positive(self, budget: str) -> None:
        env = _env() | {"CELLULAR_BUDGET_MB": budget}
        with pytest.raises(cw.ConfigError, match="CELLULAR_BUDGET_MB"):
            cw.WorkerConfig.from_env(env)

    @pytest.mark.parametrize("key", ["a b", "a\tb", "café", "a\nb"])
    def test_display_key_must_be_carriable_by_the_setting_file(self, key: str) -> None:
        env = _env() | {"CELLULAR_DISPLAY_KEY": key}
        with pytest.raises(cw.ConfigError, match="CELLULAR_DISPLAY_KEY"):
            cw.WorkerConfig.from_env(env)

    def test_intervals_are_cross_checked(self) -> None:
        env = _env() | {"CELLULAR_MIN_INTERVAL": "900"}
        with pytest.raises(cw.ConfigError, match="MIN_INTERVAL"):
            cw.WorkerConfig.from_env(env)
        env = _env() | {"CELLULAR_BACKOFF_MAX": "300"}
        with pytest.raises(cw.ConfigError, match="BACKOFF_MAX"):
            cw.WorkerConfig.from_env(env)
        env = _env() | {"CELLULAR_POLL_INTERVAL": "30"}
        with pytest.raises(cw.ConfigError, match="POLL_INTERVAL"):
            cw.WorkerConfig.from_env(env)


# -- failover ----------------------------------------------------------------


class TestFailover:
    def test_a_healthy_normal_route_never_touches_the_modem(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        for _ in range(5):
            assert h.step() == 120.0
        assert h.link.verbs == []
        assert h.fetch.events == [("normal", NORMAL_IP)] * 5
        assert h.cached is not None and h.cached.source == cache.SOURCE_NETWORK
        assert h.status is not None and h.status.state == cw.STATE_IDLE
        assert h.status.normal_error == "" and h.status.cellular_error == ""

    def test_one_lost_probe_does_not_wake_the_modem(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.step()
        h.fetch.normal = DEAD
        assert h.step() == 20.0            # confirm first, on the short retry
        assert h.link.verbs == []
        assert h.worker.status().state == cw.STATE_IDLE

    def test_second_lost_probe_engages_and_reads_over_cellular(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        _enter_fallback(h)
        assert h.fetch.events == [
            ("normal", NORMAL_IP), ("link", "route"), ("cellular", PIN), ("link", "unroute"),
        ]
        assert h.cached is not None and h.cached.source == cache.SOURCE_CELLULAR
        assert h.cached.poll_interval == 600
        assert h.status is not None and h.status.state == cw.STATE_FALLBACK
        assert h.status.cellular_error == "" and h.status.last_source == cache.SOURCE_CELLULAR

    def test_a_transient_blip_never_engages(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.step()
        h.fetch.normal = DEAD
        h.step()
        h.fetch.normal = ok()
        h.step()
        h.step()
        assert h.link.verbs == []
        assert h.cached is not None and h.cached.source == cache.SOURCE_NETWORK

    @pytest.mark.parametrize("status", [401, 403, 429, 500, 503, 302])
    def test_an_answering_server_is_reachable_whatever_it_says(
        self, tmp_path: Path, status: int
    ) -> None:
        # Authentication and rate-limit answers are not connectivity failures;
        # cellular could not change them, so they are surfaced and never retried
        # over the metered link.
        h = Harness(tmp_path)
        h.fetch.normal = http(status)
        for _ in range(4):
            assert h.step() == 120.0
        assert h.link.verbs == []
        assert h.cached is None
        assert h.status is not None and h.status.state == cw.STATE_IDLE
        assert h.status.normal_error == f"http_{status}"
        assert h.status.cellular_error == ""

    def test_a_name_that_does_not_resolve_is_unreachable(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.step()
        h.fetch.candidates = []
        h.step()
        h.step()
        assert h.link.verbs == ["route", "unroute"]
        # The two paths fail independently and are reported independently:
        # the panel is on cellular *because* of "dns".
        assert h.status is not None
        assert h.status.normal_error == "dns" and h.status.cellular_error == ""

    def test_one_dead_address_of_several_still_reaches_the_dashboard(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.fetch.candidates = ["203.0.113.9", NORMAL_IP]
        answers = {"203.0.113.9": DEAD, NORMAL_IP: ok()}
        h.fetch.read = lambda ip, *, cellular: answers[ip]  # type: ignore[method-assign]
        for _ in range(4):
            h.step()
        assert h.link.verbs == []

    def test_recovery_returns_to_the_normal_route_and_takes_the_link_down(
        self, tmp_path: Path
    ) -> None:
        h = Harness(tmp_path)
        _enter_fallback(h)
        h.fetch.normal = ok(Provider.CLAUDE)
        assert h.step() == 120.0
        assert h.link.verbs == ["route", "unroute", "down"]
        assert h.cached is not None and h.cached.source == cache.SOURCE_NETWORK
        assert h.status is not None and h.status.state == cw.STATE_IDLE
        assert h.status.normal_error == "" and h.status.cellular_error == ""
        # And it stays down: healthy cycles make no further link calls.
        h.step()
        h.step()
        assert h.link.verbs == ["route", "unroute", "down"]

    def test_the_cellular_route_is_held_only_around_a_cellular_read(
        self, tmp_path: Path
    ) -> None:
        # The load-bearing ordering. The normal-route probe that decides whether
        # to stay in fallback happens *after* the previous unroute and *before*
        # the next route, so it is answered by the normal route or not at all.
        h = Harness(tmp_path)
        _enter_fallback(h)
        h.step()
        assert h.fetch.events == [
            ("normal", NORMAL_IP), ("link", "route"), ("cellular", PIN), ("link", "unroute"),
            ("normal", NORMAL_IP), ("link", "route"), ("cellular", PIN), ("link", "unroute"),
        ]
        # The link itself stays up between reads; only the selection toggles.
        assert "down" not in h.link.verbs

    def test_recovery_is_preferred_over_a_working_cellular_link(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        _enter_fallback(h)
        # Normal route answers again but the cellular one still works too.
        h.fetch.normal = ok(Provider.CLAUDE)
        h.fetch.cellular = ok(Provider.ZAI)
        h.step()
        assert h.link.verbs == ["route", "unroute", "down"]
        assert h.cached is not None and h.cached.source == cache.SOURCE_NETWORK

    def test_every_pinned_address_is_tried_before_giving_up(self, tmp_path: Path) -> None:
        h = Harness(tmp_path, destinations=(PIN, "192.0.2.11", "192.0.2.12", "192.0.2.13"))
        answers = {PIN: DEAD, "192.0.2.11": DEAD, "192.0.2.12": ok(), "192.0.2.13": ok()}
        def read(ip: str, *, cellular: bool) -> cw.FetchOutcome:
            h.fetch.events.append(("cellular" if cellular else "normal", ip))
            return answers[ip] if ip in answers else DEAD   # the normal route is dead

        h.fetch.read = read  # type: ignore[method-assign]
        _enter_fallback(h)
        # Bounded: no more than three attempts per read, and it stops at the
        # first address that answers.
        assert h.cellular_reads() == [PIN, "192.0.2.11", "192.0.2.12"]
        assert h.cached is not None


# -- when the modem may not be used -------------------------------------------


class TestRestraint:
    def test_a_dark_panel_never_spends_data(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.signals.is_awake = False
        h.step()
        h.fetch.normal = DEAD
        # One short confirming probe, then the slow free probe cadence.
        assert [h.step() for _ in range(5)] == [20.0, 120.0, 120.0, 120.0, 120.0]
        assert h.link.verbs == []
        assert h.cellular_reads() == []
        assert h.worker.status().state == cw.STATE_FALLBACK

    def test_going_dark_takes_an_active_link_down(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        _enter_fallback(h)
        h.fetch.events.clear()
        h.signals.is_awake = False
        assert h.step() == 120.0
        assert h.link.verbs == ["down"]
        assert [ip for kind, ip in h.fetch.events if kind == "cellular"] == []

    def test_an_unset_clock_blocks_cellular(self, tmp_path: Path) -> None:
        # Cold boot, no working link, so no time sync: HTTPS cannot validate
        # and every attempt would spend data on a doomed handshake.
        h = Harness(tmp_path)
        h.step()
        h.signals.unsynced = True
        h.fetch.normal = DEAD
        h.step()
        h.step()
        h.step()
        assert h.link.verbs == []
        assert h.status is not None and h.status.state == cw.STATE_CLOCK

    def test_a_failed_link_attempt_never_reads(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.link.results["route"] = cw.LinkResult(False, "absent")
        h.fetch.normal = DEAD
        h.step()
        h.step()
        # Nothing to undo for this reason — but the worker cannot know that,
        # so it asks for `down` after every failed `route`.
        assert h.link.verbs == ["route", "down"]
        assert h.cellular_reads() == []
        assert h.cached is None
        assert h.status is not None and h.status.cellular_error == "link_absent"
        assert h.status.state == cw.STATE_FALLBACK

    def test_link_failures_back_off_and_recover(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.link.results["route"] = cw.LinkResult(False, "absent")
        h.fetch.normal = DEAD
        h.step()
        waits = [h.step() for _ in range(6)]
        assert waits == [20.0, 40.0, 80.0, 160.0, 320.0, 640.0]
        # The modem is re-plugged: the next attempt succeeds and reads at once.
        del h.link.results["route"]
        assert h.step() == 600.0
        assert h.cached is not None and h.cached.source == cache.SOURCE_CELLULAR
        assert h.link.verbs[-2:] == ["route", "unroute"]

    def test_if_the_selection_cannot_be_withdrawn_the_link_is_taken_down(
        self, tmp_path: Path
    ) -> None:
        h = Harness(tmp_path)
        h.link.results["unroute"] = cw.LinkResult(False, "busy")
        h.fetch.normal = DEAD
        h.step()
        h.step()
        assert h.link.verbs == ["route", "unroute", "down"]
        # The payload was still read, but nothing else may use the link.
        assert h.cached is not None and h.cached.source == cache.SOURCE_CELLULAR

    def test_if_the_link_cannot_be_taken_down_the_worker_keeps_trying(
        self, tmp_path: Path
    ) -> None:
        h = Harness(tmp_path)
        h.link.results["down"] = cw.LinkResult(False, "busy")
        h.fetch.normal = DEAD
        h.step()
        h.step()
        h.signals.is_awake = False
        h.step()
        h.step()
        assert h.link.verbs.count("down") == 2


# -- backoff ------------------------------------------------------------------


class TestBackoff:
    def test_cellular_read_failures_double_to_a_cap_and_reset(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        _enter_fallback(h)
        first = h.cached
        assert first is not None
        h.fetch.cellular = DEAD
        waits = [h.step() for _ in range(5)]
        assert waits == [1200.0, 2400.0, 3600.0, 3600.0, 3600.0]
        # The last good payload is preserved, not replaced or cleared.
        assert h.cached == first
        h.fetch.cellular = ok()
        assert h.step() == 600.0
        assert h.cached is not None and h.cached != first

    def test_a_refused_cellular_read_is_not_retried_soon(self, tmp_path: Path) -> None:
        # 401 here means the display credential is wrong. The link works; only
        # the hour-long backoff will ask again.
        h = Harness(tmp_path)
        _enter_fallback(h)
        first = h.cached
        h.fetch.cellular = http(401)
        assert h.step() == 3600.0
        assert h.status is not None and h.status.cellular_error == "http_401"
        assert h.cached == first

    def test_a_200_that_is_not_the_snapshot_is_not_published(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        _enter_fallback(h)
        first = h.cached
        h.fetch.cellular = cw.FetchOutcome(cw.HTTP, status=200, reason="payload")
        assert h.step() == 1200.0
        assert h.cached == first


# -- data budget ----------------------------------------------------------------


class TestBudget:
    def test_over_budget_the_link_stays_down_but_normal_probes_continue(
        self, tmp_path: Path
    ) -> None:
        h = Harness(tmp_path)
        h.signals.counter = (0, 0)
        _enter_fallback(h)
        h.signals.counter = (60_000_000, 100)  # already past 50 MB
        assert h.step() == 600.0
        # Noticed at the baseline reading, before the read is even started.
        assert h.link.verbs == ["route", "unroute", "route", "down"]
        assert h.status is not None and h.status.state == cw.STATE_BUDGET
        assert h.status.bytes_used == 60_000_100
        # Over budget, the modem is not touched again...
        assert h.step() == 600.0
        assert h.link.verbs == ["route", "unroute", "route", "down"]
        # ...while a working normal route is still used and still published.
        h.fetch.normal = ok()
        h.step()
        assert h.cached is not None and h.cached.source == cache.SOURCE_NETWORK

    def test_bytes_are_counted_from_the_interface_counters(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.signals.counter = (0, 0)
        _enter_fallback(h)
        h.signals.counter = (9_000, 2_000)      # idle time + this read
        h.step()
        h.signals.counter = (12_000, 3_000)     # idle time + next read
        h.step()
        assert h.status is not None and h.status.bytes_used == 15_000
        saved = json.loads((tmp_path / cache.USAGE_FILE).read_text())
        assert saved["bytes"] == 15_000 and saved["last_counter"] == [12000, 3000]

    def test_crossing_the_budget_mid_read_releases_the_link(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.signals.counter = (0, 0)
        _enter_fallback(h)
        real_read = h.fetch.read

        def expensive_read(ip: str, *, cellular: bool) -> cw.FetchOutcome:
            if cellular:
                # The read itself is what tips the month over 50 MB.
                h.signals.counter = (49_999_000, 5_000)
            return real_read(ip, cellular=cellular)

        h.fetch.read = expensive_read  # type: ignore[method-assign]
        assert h.step() == 600.0
        assert h.link.verbs == ["route", "unroute", "route", "unroute", "down"]
        assert h.status is not None and h.status.state == cw.STATE_BUDGET
        assert h.status.bytes_used == 50_004_000

    def test_usage_survives_a_restart(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.signals.counter = (0, 0)
        _enter_fallback(h)
        h.signals.counter = (4_000, 1_000)
        h.step()
        h2 = Harness(tmp_path)                   # same state dir, new process
        assert h2.status is not None and h2.status.bytes_used == 5_000
        # ...and the restart's own `down` is the only link call it makes.
        assert h2.link.verbs == []
        # Nothing is counted while the link is down: the idle reading is not
        # folded in until the next link operation.
        h2.signals.counter = (4_500, 1_000)
        h2.step()
        assert h2.status is not None and h2.status.bytes_used == 5_000

    def test_a_re_enumerated_interface_does_not_invent_bytes(self, tmp_path: Path) -> None:
        # Hot-plugged HAT: the counters restart from zero.
        h = Harness(tmp_path)
        h.signals.counter = (1_000_000, 500_000)
        _enter_fallback(h)
        h.signals.counter = (300, 120)
        h.step()
        assert h.status is not None and h.status.bytes_used == 0

    def test_a_new_month_starts_the_budget_over(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.signals.counter = (0, 0)
        _enter_fallback(h)
        h.signals.counter = (60_000_000, 0)
        h.step()
        assert h.worker.status().state == cw.STATE_BUDGET
        h.clock.wall += 32 * 24 * 3600
        h.signals.counter = (60_000_500, 100)
        assert h.step() == 600.0
        assert h.link.verbs[-2:] == ["route", "unroute"]
        assert h.status is not None and h.status.state == cw.STATE_FALLBACK
        assert h.status.bytes_used == 600


# -- GUI signals: wake and refresh ----------------------------------------------


class TestWakeAndRefresh:
    def test_waking_cuts_the_wait_short(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.signals.is_awake = False
        ticks: list[float] = []

        def sleep(seconds: float) -> bool:
            ticks.append(seconds)
            # The panel lights up a moment into the wait.
            if len(ticks) == 3:
                h.signals.is_awake = True
            h.clock.advance(seconds)
            return False

        h.worker._sleep = sleep  # noqa: SLF001
        assert h.worker._pause(600.0) == "wake"  # noqa: SLF001
        assert sum(ticks) == 1.5

    def test_waking_while_the_panel_is_already_lit_is_not_a_wake(
        self, tmp_path: Path
    ) -> None:
        h = Harness(tmp_path)
        assert h.worker._pause(2.0) is None  # noqa: SLF001
        assert h.clock.wall == NOW + 2.0

    def test_stop_interrupts_the_wait(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.worker._sleep = lambda seconds: True  # noqa: SLF001
        assert h.worker._pause(600.0) == "stop"  # noqa: SLF001

    def test_a_refresh_tap_during_the_minimum_gap_is_declined(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        _enter_fallback(h)
        h.signals.refresh_tokens = [101.0]
        # 10 s after the last cellular read: inside the 60 s minimum gap.
        h.clock.mono = h.read_at + 10
        assert h.worker._pause(600.0) is None  # noqa: SLF001
        assert h.status is not None and h.status.request == cache.RequestOutcome(101.0, "deferred")
        assert h.link.verbs == ["route", "unroute"]   # nothing was re-read

    def test_a_refresh_tap_after_the_gap_reads_now(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        _enter_fallback(h)
        h.clock.mono = h.read_at + 61
        before = h.clock.wall
        h.signals.refresh_tokens = [102.0]
        assert h.worker._pause(600.0) == "refresh"  # noqa: SLF001
        assert h.clock.wall == before              # answered without waiting
        assert h.step() == 600.0
        assert h.link.verbs == ["route", "unroute", "route", "unroute"]

    def test_a_refresh_tap_is_answered_once(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.signals.refresh_tokens = [103.0]
        assert h.worker._pause(1.0) == "refresh"  # noqa: SLF001
        assert h.worker._pause(1.0) is None       # noqa: SLF001

    def test_a_refresh_tap_on_a_healthy_unit_reads_the_normal_route(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.step()
        h.clock.advance(30)
        h.signals.refresh_tokens = [104.0]
        assert h.worker._pause(120.0) == "refresh"  # noqa: SLF001
        h.step()
        assert h.link.verbs == []
        assert h.fetch.events == [("normal", NORMAL_IP), ("normal", NORMAL_IP)]


# -- lifetime ------------------------------------------------------------------


class TestLifetime:
    def _run(self, h: Harness, pauses_before_stop: int) -> list[float]:
        """Run the worker's loop until it has asked to wait that many times."""
        pauses: list[float] = []
        real_pause = h.worker._pause  # noqa: SLF001

        def pause(seconds: float) -> str | None:
            pauses.append(seconds)
            if len(pauses) >= pauses_before_stop:
                h.worker.stop()
            return real_pause(seconds)

        h.worker._pause = pause  # noqa: SLF001
        h.worker.run()
        return pauses

    def test_the_link_is_taken_down_on_the_way_in_and_on_the_way_out(
        self, tmp_path: Path
    ) -> None:
        # Stopping while healthy: the modem was taken down on the way in and
        # is never touched again.
        h = Harness(tmp_path)
        assert self._run(h, 4) == [120.0, 120.0, 120.0, 120.0]
        # In: the deny policy, then the link taken down in case a predecessor
        # left it up. Healthy cycles and a clean stop touch neither again.
        assert h.link.verbs == ["firewall", "down"]
        assert h.fetch.closed
        final = cache.read_status(tmp_path)
        assert final is not None and final.next_poll_at is None

    def test_stopping_while_in_fallback_takes_the_active_link_down(
        self, tmp_path: Path
    ) -> None:
        h = Harness(tmp_path)
        h.fetch.normal = DEAD
        assert self._run(h, 4) == [20.0, 600.0, 600.0, 600.0]
        # in: firewall, down | confirm, engage, two fallback reads | out: down
        assert h.link.verbs == [
            "firewall", "down",
            "route", "unroute", "route", "unroute", "route", "unroute",
            "down",
        ]

    def test_a_crashing_cycle_does_not_stop_the_worker(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)

        def crash() -> float:
            raise RuntimeError("bug in one pass")

        h.worker.cycle = crash  # type: ignore[method-assign]
        assert self._run(h, 3) == [20.0, 20.0, 20.0]   # the short retry, each time
        # On the way in, and after each of the three passes.
        assert h.link.verbs == ["firewall", "down", "down", "down", "down"]

    def test_a_missing_setting_exits_with_the_config_code(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        for name in ("CELLULAR_DISPLAY_KEY", "CELLULAR_DEST", "CELLULAR_IFACE",
                     "CELLULAR_BUDGET_MB"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("SERVER_URL", "https://dash.example")
        with caplog.at_level(logging.ERROR):
            assert cw.main([]) == cw.EXIT_CONFIG
        assert "CELLULAR_DISPLAY_KEY" in caplog.text


# -- the HTTP request itself ------------------------------------------------------


def _cfg(tmp_path: Path) -> cw.WorkerConfig:
    return _base_cfg(tmp_path)


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestRequest:
    def test_goes_to_the_literal_but_is_addressed_to_the_hostname(self, tmp_path: Path) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=_payload(), request=request)

        outcome = cw._request(_client(handler), _cfg(tmp_path), PIN)
        assert outcome.ok
        request = seen[0]
        # The address is the pinned one — no name lookup on the metered path...
        assert str(request.url) == f"https://{PIN}/api/v1/readings"
        # ...while the verified name and the virtual host are the real ones.
        assert request.headers["host"] == HOST
        assert request.extensions["sni_hostname"] == HOST.encode("ascii")
        assert request.headers["authorization"] == "Bearer display-test"

    def test_an_ipv6_literal_is_bracketed(self, tmp_path: Path) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=[], request=request)

        cw._request(_client(handler), _cfg(tmp_path), PIN6)
        assert seen[0].url.host == PIN6
        assert seen[0].headers["host"] == HOST

    def test_a_redirect_is_reported_not_followed(self, tmp_path: Path) -> None:
        asked: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            asked.append(request.url.path)
            return httpx.Response(302, headers={"location": "https://elsewhere.example/x"},
                                  request=request)

        outcome = cw._request(_client(handler), _cfg(tmp_path), PIN)
        assert outcome.reachable and not outcome.ok
        assert outcome.status == 302
        assert asked == [cw.READINGS_PATH]

    @pytest.mark.parametrize(
        "body,reason",
        [
            ('{"readings": []}', "payload_200"),
            ("not json", "payload_200"),
            ('[{"status": "current"}]', "payload_200"),
            ("[1, 2]", "payload_200"),
        ],
    )
    def test_a_200_that_is_not_the_snapshot_is_not_success(
        self, tmp_path: Path, body: str, reason: str
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=body, request=request)

        outcome = cw._request(_client(handler), _cfg(tmp_path), PIN)
        assert outcome.reachable and not outcome.ok and outcome.label() == reason

    @pytest.mark.parametrize("status", [401, 403, 429, 500])
    def test_error_statuses_are_reachable_not_ok(self, tmp_path: Path, status: int) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, request=request)

        outcome = cw._request(_client(handler), _cfg(tmp_path), PIN)
        assert outcome.reachable and not outcome.ok and outcome.label() == f"http_{status}"

    @pytest.mark.parametrize(
        "raised,reason",
        [
            (httpx.ConnectTimeout("t"), "timeout"),
            (httpx.ReadTimeout("t"), "timeout"),
            (httpx.ConnectError("refused"), "connect"),
            (httpx.ReadError("reset"), "io"),
            (httpx.RemoteProtocolError("bogus"), "protocol"),
        ],
    )
    def test_transport_failures_are_named_by_class(
        self, tmp_path: Path, raised: Exception, reason: str
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise raised

        outcome = cw._request(_client(handler), _cfg(tmp_path), PIN)
        assert not outcome.reachable and outcome.label() == reason

    @pytest.mark.parametrize(
        "inner,reason",
        [
            (ssl.SSLCertVerificationError(1, "certificate verify failed"), "tls_verify"),
            (ssl.SSLError("handshake"), "tls"),
        ],
    )
    def test_tls_failures_are_distinguished_from_an_absent_route(
        self, tmp_path: Path, inner: ssl.SSLError, reason: str
    ) -> None:
        # "connect" would send the worker looking for a route problem; a TLS
        # failure most often means the clock is not set.
        def handler(request: httpx.Request) -> httpx.Response:
            try:
                raise inner
            except ssl.SSLError as exc:
                raise httpx.ConnectError("handshake failed", request=request) from exc

        outcome = cw._request(_client(handler), _cfg(tmp_path), PIN)
        assert not outcome.reachable and outcome.label() == reason


class TestHttpFetcher:
    def test_each_route_has_its_own_connections(self, tmp_path: Path) -> None:
        pools: list[list[str]] = []

        def factory() -> httpx.Client:
            mine: list[str] = []
            pools.append(mine)

            def handler(request: httpx.Request) -> httpx.Response:
                mine.append(request.url.host)
                return httpx.Response(200, json=_payload(), request=request)

            return httpx.Client(transport=httpx.MockTransport(handler))

        fetcher = cw.HttpFetcher(_cfg(tmp_path), client_factory=factory,
                                 resolve=lambda host, port: [NORMAL_IP])
        fetcher.read(NORMAL_IP, cellular=False)
        fetcher.read(NORMAL_IP, cellular=False)
        fetcher.read(PIN, cellular=True)
        assert pools[0] == [NORMAL_IP, NORMAL_IP]   # reused on its own route
        assert pools[1] == [PIN]                     # never the Wi-Fi pool


    def test_only_cellular_reads_use_the_relay_port(self, tmp_path: Path) -> None:
        seen: list[tuple[bool, str, int]] = []

        def factory() -> httpx.Client:
            def handler(request: httpx.Request) -> httpx.Response:
                port = request.url.port or 443   # httpx drops the scheme's default
                seen.append((port == 8443, request.headers["host"], port))
                return httpx.Response(200, json=_payload(), request=request)

            return httpx.Client(transport=httpx.MockTransport(handler))

        cfg = replace(_cfg(tmp_path), dest_port=8443)
        fetcher = cw.HttpFetcher(cfg, client_factory=factory,
                                 resolve=lambda host, port: [NORMAL_IP])
        fetcher.read(NORMAL_IP, cellular=False)
        fetcher.read(PIN, cellular=True)
        assert seen == [(False, HOST, 443), (True, HOST, 8443)]


# -- bounded name resolution ---------------------------------------------------


class TestBoundedResolver:
    def test_a_literal_is_returned_as_is(self) -> None:
        def never(host: str, port: int) -> list[str]:
            raise AssertionError("must not resolve a literal")

        assert cw.BoundedResolver(1.0, lookup=never).resolve(PIN, 443) == [PIN]
        assert cw.BoundedResolver(1.0, lookup=never).resolve("2001:DB8::1", 443) == ["2001:db8::1"]

    def test_a_name_is_resolved_and_deduplicated_in_order(self) -> None:
        answer = [NORMAL_IP, NORMAL_IP, "2001:db8::7"]
        resolver = cw.BoundedResolver(1.0, lookup=lambda h, p: answer)
        assert resolver.resolve(HOST, 443) == [NORMAL_IP, "2001:db8::7"]

    def test_a_hanging_lookup_is_bounded(self) -> None:
        release = threading.Event()

        def stuck(host: str, port: int) -> list[str]:
            release.wait(5)
            return [NORMAL_IP]

        resolver = cw.BoundedResolver(0.05, lookup=stuck)
        started = time.monotonic()
        assert resolver.resolve(HOST, 443) is None
        assert time.monotonic() - started < 1.0
        # The stuck lookup still holds the resolver: the next call fails fast
        # instead of piling up another blocked thread.
        assert resolver.resolve(HOST, 443) is None
        assert time.monotonic() - started < 1.0
        release.set()
        time.sleep(0.05)

    def test_the_last_good_answer_is_reused_while_the_resolver_is_stuck(self) -> None:
        answers = {"next": [[NORMAL_IP], None]}
        gate = threading.Event()

        def lookup(host: str, port: int) -> list[str]:
            plan = answers["next"].pop(0)
            if plan is None:
                gate.wait(5)
            return plan or []

        resolver = cw.BoundedResolver(0.05, lookup=lookup)
        assert resolver.resolve(HOST, 443) == [NORMAL_IP]
        assert resolver.resolve(HOST, 443) == [NORMAL_IP]   # hung; stale answer
        gate.set()
        time.sleep(0.05)

    def test_a_failed_lookup_after_a_good_one_keeps_the_last_answer(self) -> None:
        plans: list[Any] = [[NORMAL_IP], OSError("temporary failure")]
        resolver = cw.BoundedResolver(1.0, lookup=lambda h, p: _raise_or_return(plans.pop(0)))
        assert resolver.resolve(HOST, 443) == [NORMAL_IP]
        assert resolver.resolve(HOST, 443) == [NORMAL_IP]


def _raise_or_return(plan: Any) -> list[str]:
    if isinstance(plan, Exception):
        raise plan
    return plan


# -- the privileged helper call ----------------------------------------------------


class TestSudoLink:
    def _ran(
        self, tmp_path: Path, result: Any, *, raises: bool = False
    ) -> tuple[cw.LinkResult, Any]:
        calls: list[Any] = []

        def runner(command: list[str], **options: Any) -> Any:
            calls.append((command, options))
            if raises:
                raise result
            return result

        outcome = cw.SudoLink("/opt/helper", runner=runner)("route")
        return outcome, calls[0]

    def test_it_runs_exactly_one_word_through_a_non_interactive_sudo(self, tmp_path: Path) -> None:
        outcome, (command, options) = self._ran(
            tmp_path, subprocess.CompletedProcess([], 0, "", "")
        )
        assert outcome.ok
        assert command == ["sudo", "-n", "/opt/helper", "route"]
        assert options["check"] is False and options["timeout"] == 150.0

    def test_the_helpers_reason_is_its_first_line(self, tmp_path: Path) -> None:
        outcome, _ = self._ran(
            tmp_path,
            subprocess.CompletedProcess([], 1, "", "profile: ipv4.never-default is no\nmore\n"),
        )
        assert outcome == cw.LinkResult(False, "profile: ipv4.never-default is no")

    def test_a_timeout_and_a_missing_helper_are_reported(self, tmp_path: Path) -> None:
        outcome, _ = self._ran(tmp_path, subprocess.TimeoutExpired("cmd", 150), raises=True)
        assert outcome == cw.LinkResult(False, "timeout")
        outcome, _ = self._ran(tmp_path, FileNotFoundError("sudo"), raises=True)
        assert outcome.reason == "helper_FileNotFoundError" and not outcome.ok

    def test_an_exit_without_a_message_still_names_it(self, tmp_path: Path) -> None:
        outcome, _ = self._ran(tmp_path, subprocess.CompletedProcess([], 3, "", ""))
        assert outcome == cw.LinkResult(False, "exit_3")

    def test_the_firewall_verb_reaches_sudo_too(self, tmp_path: Path) -> None:
        # Loading the deny policy cannot let anything through, so the worker
        # may ask for it at any time.
        calls: list[Any] = []

        def runner(command: list[str], **options: Any) -> Any:
            calls.append((command, options))
            return subprocess.CompletedProcess([], 0, "", "")

        assert cw.SudoLink("/opt/helper", runner=runner)("firewall").ok
        assert calls[0][0] == ["sudo", "-n", "/opt/helper", "firewall"]
        assert cw.SudoLink._TIMEOUTS["firewall"] < cw.SudoLink._TIMEOUTS["route"]

    def test_an_unknown_verb_never_reaches_sudo(self, tmp_path: Path) -> None:
        def runner(command: list[str], **options: Any) -> Any:
            raise AssertionError("must not run")

        assert cw.SudoLink("/opt/helper", runner=runner)("status") == cw.LinkResult(False, "verb")


# -- what the worker reads from the unit ------------------------------------------------


class TestFileSignals:
    def _signals(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: dict[str, float]
    ) -> cw.FileSignals:
        monkeypatch.setattr(cw.time, "time", lambda: clock["now"])
        (tmp_path / "cell0" / "statistics").mkdir(parents=True, exist_ok=True)
        (tmp_path / "cell0" / "statistics" / "rx_bytes").write_text("1234\n")
        (tmp_path / "cell0" / "statistics" / "tx_bytes").write_text("567\n")
        return cw.FileSignals(
            tmp_path, tmp_path / "request", "cell0", 150.0, sysfs_net=tmp_path
        )

    def test_interface_counters_come_from_sysfs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        signals = self._signals(tmp_path, monkeypatch, {"now": NOW})
        assert signals.iface_bytes() == (1234, 567)
        (tmp_path / "cell0" / "statistics" / "rx_bytes").unlink()
        assert signals.iface_bytes() is None   # modem absent

    def test_the_clock_is_trusted_only_once_a_payload_has_been_verified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        published = datetime.fromtimestamp(NOW, timezone.utc).replace(tzinfo=None)
        cache.publish(
            cache.CacheEntry(published, cache.SOURCE_CELLULAR, 600, []), tmp_path
        )
        # A Pi without a real-time clock starts from its last synchronisation,
        # so at boot it is behind by however long it was powered off. A little
        # of that is ordinary; HTTPS validated on the work Pi a week behind.
        slightly_behind = self._signals(tmp_path, monkeypatch, {"now": NOW - 60})
        assert slightly_behind.unsynced_clock() is False
        within_tolerance = self._signals(
            tmp_path, monkeypatch, {"now": NOW - cw.CLOCK_SKEW_TOLERANCE_SECONDS + 1}
        )
        assert within_tolerance.unsynced_clock() is False
        # Far behind is a clock that was never set, and those handshakes
        # cannot validate.
        far_behind = self._signals(
            tmp_path, monkeypatch, {"now": NOW - cw.CLOCK_SKEW_TOLERANCE_SECONDS - 1}
        )
        assert far_behind.unsynced_clock() is True
        ahead = self._signals(tmp_path, monkeypatch, {"now": NOW + 60})
        assert ahead.unsynced_clock() is False

    def test_without_a_published_payload_the_clock_is_not_judged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert self._signals(tmp_path, monkeypatch, {"now": 0.0}).unsynced_clock() is False

    def test_an_old_refresh_request_is_not_replayed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache.request_refresh(tmp_path / "request", 5.0)
        signals = self._signals(tmp_path, monkeypatch, {"now": NOW})
        assert signals.take_refresh() is None
        cache.request_refresh(tmp_path / "request", 6.0)
        assert signals.take_refresh() == 6.0
        assert signals.take_refresh() is None

    def test_awake_follows_the_heartbeat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = {"now": NOW}
        signals = self._signals(tmp_path, monkeypatch, clock)
        assert signals.awake() is False
        cache.touch_awake(tmp_path / "request")
        mtime = (tmp_path / "request" / cache.AWAKE_FILE).stat().st_mtime
        clock["now"] = mtime + 10
        assert signals.awake() is True
        clock["now"] = mtime + 600
        assert signals.awake() is False


# -- usage ledger on its own --------------------------------------------------------------


class TestUsageLedger:
    def test_unknown_or_damaged_files_start_at_zero(self, tmp_path: Path) -> None:
        (tmp_path / cache.USAGE_FILE).write_text("{")
        assert cw.UsageLedger(tmp_path / cache.USAGE_FILE, 10, lambda: NOW).bytes == 0

    def test_a_saved_ledger_from_another_month_is_not_carried_over(self, tmp_path: Path) -> None:
        path = tmp_path / cache.USAGE_FILE
        cw.UsageLedger(path, 10, lambda: NOW).observe((7, 7))
        later = NOW + 32 * 24 * 3600
        assert cw.UsageLedger(path, 10, lambda: later).bytes == 0


# -- a refused route (an independent review found this hole) ----------------------------------


class TestRefusedRoute:
    @pytest.mark.parametrize("reason", ["select", "leak", "timeout", "conflict", "policy"])
    def test_every_failed_route_is_followed_by_down(
        self, tmp_path: Path, reason: str
    ) -> None:
        # A helper killed on its deadline, or one from before it unwound its
        # own refusals, can leave the worker's routing rule installed. The
        # worker then believes the link is down and asks whether the normal
        # route recovered — and that question would leave over cellular,
        # uncounted, and come back as a recovery that did not happen. So a
        # failed `route` is always followed by `down`, whatever the reason.
        h = Harness(tmp_path)
        h.link.results["route"] = cw.LinkResult(False, reason)
        h.fetch.normal = DEAD
        h.step()
        h.step()
        assert h.link.verbs[-2:] == ["route", "down"]
        assert h.cellular_reads() == []
        # And the next thing it does is ask the normal route again.
        h.fetch.events.clear()
        h.link.verbs.clear()
        h.step()
        assert h.fetch.events[0] == ("normal", NORMAL_IP)
        assert "route" not in h.link.verbs or h.link.verbs[-1] == "down"

    def test_a_route_that_raises_is_followed_by_down_too(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.link.results["route"] = RuntimeError("helper crashed")
        h.fetch.normal = DEAD
        h.step()
        h.step()
        assert h.link.verbs[-2:] == ["route", "down"]
        assert h.status is not None
        assert h.status.cellular_error == "link_helper_RuntimeError"

    def test_a_down_that_fails_is_asked_for_again(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.link.results["route"] = cw.LinkResult(False, "select")
        h.link.results["down"] = cw.LinkResult(False, "busy")
        h.fetch.normal = DEAD
        h.step()
        h.step()                      # route refused, down refused
        h.signals.is_awake = False
        h.step()
        h.step()
        # Never assumed clean: every later cycle tries again.
        assert h.link.verbs.count("down") == 3

    def test_the_link_backoff_grows_even_when_there_was_nothing_to_undo(
        self, tmp_path: Path
    ) -> None:
        h = Harness(tmp_path)
        h.link.results["route"] = cw.LinkResult(False, "absent")
        h.fetch.normal = DEAD
        h.step()
        # h.step() above was the probe that confirms the normal route is dead;
        # what follows is the ladder of link attempts, each still doubled by
        # the `down` that follows it.
        assert [h.step() for _ in range(5)] == [20.0, 40.0, 80.0, 160.0, 320.0]
        assert h.link.verbs == ["route", "down"] * 5


class TestRefreshTapsRespectRefusals:
    def _in_fallback_after(self, h: Harness, cellular: cw.FetchOutcome) -> None:
        _enter_fallback(h)
        h.fetch.cellular = cellular
        h.step()
        h.fetch.events.clear()
        h.link.verbs.clear()
        h.clock.mono = h.read_at + 3600 + 3600     # past the last read's wait...

    def test_a_tap_after_a_refused_read_does_not_shorten_the_hour(
        self, tmp_path: Path
    ) -> None:
        # A 401 means the credential is wrong: one more read cannot fix that,
        # and the plan says such answers are not retried over cellular.
        h = Harness(tmp_path)
        _enter_fallback(h)
        h.fetch.cellular = http(401)
        assert h.step() == 3600.0
        h.fetch.events.clear()
        h.clock.mono += 120                       # two minutes into that hour
        h.signals.refresh_tokens = [201.0]
        assert h.worker._pause(3480.0) is None  # noqa: SLF001
        assert h.status is not None
        assert h.status.request == cache.RequestOutcome(201.0, "deferred")
        assert h.cellular_reads() == []

    def test_a_tap_during_a_failure_backoff_is_declined(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        _enter_fallback(h)
        h.fetch.cellular = DEAD
        assert h.step() == 1200.0
        h.fetch.events.clear()
        h.clock.mono += 120
        h.signals.refresh_tokens = [202.0]
        assert h.worker._pause(1080.0) is None  # noqa: SLF001
        assert h.status is not None
        assert h.status.request == cache.RequestOutcome(202.0, "deferred")
        assert h.cellular_reads() == []

    def test_a_tap_after_a_refused_link_is_declined(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        _enter_fallback(h)
        h.link.results["route"] = cw.LinkResult(False, "absent")
        h.step()
        h.clock.mono += 120
        h.signals.refresh_tokens = [203.0]
        assert h.worker._pause(60.0) is None  # noqa: SLF001
        assert h.status is not None
        assert h.status.request == cache.RequestOutcome(203.0, "deferred")

    def test_once_a_read_succeeds_again_taps_are_honoured(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        _enter_fallback(h)
        h.fetch.cellular = http(401)
        h.step()
        h.clock.mono += 3600
        h.fetch.cellular = ok()
        assert h.step() == 600.0
        h.clock.mono += 61                           # past the minimum gap
        h.signals.refresh_tokens = [204.0]
        assert h.worker._pause(540.0) == "refresh"  # noqa: SLF001


class TestLedgerVersusTheClock:
    def test_a_restart_with_the_clock_behind_keeps_the_ledger(self, tmp_path: Path) -> None:
        # First boot of a month before NTP has run: the wall clock reads an
        # earlier month than the ledger was written under. That is the clock
        # being wrong, not a new bill.
        path = tmp_path / cache.USAGE_FILE
        ledger = cw.UsageLedger(path, 50_000_000, lambda: NOW)
        ledger.observe((0, 0))                     # the baseline
        ledger.observe((40_000_000, 0))
        assert ledger.bytes == 40_000_000
        restarted = cw.UsageLedger(path, 50_000_000, lambda: 1_000_000.0)   # 1970
        assert restarted.bytes == 40_000_000
        assert restarted.month == "2027-01"
        restarted.observe((40_000_600, 0))
        assert restarted.bytes == 40_000_600
        # Then the clock is set, to the same month: still the same ledger.
        restarted._now = lambda: NOW
        restarted.observe((40_000_700, 0))
        assert restarted.bytes == 40_000_700 and restarted.month == "2027-01"

    def test_a_genuinely_later_month_starts_afresh(self, tmp_path: Path) -> None:
        path = tmp_path / cache.USAGE_FILE
        ledger = cw.UsageLedger(path, 50_000_000, lambda: NOW)
        ledger.observe((0, 0))
        ledger.observe((40_000_000, 0))
        february = cw.UsageLedger(path, 50_000_000, lambda: NOW + 32 * 86400)
        assert february.month == "2027-02"
        february.observe((41_000_000, 0))           # a fresh ledger starts with a baseline
        february.observe((42_000_000, 0))
        assert february.bytes == 1_000_000          # January's spend is gone

    def test_a_clock_that_steps_backwards_does_not_restore_spent_bytes(
        self, tmp_path: Path
    ) -> None:
        clock = {"now": NOW}
        ledger = cw.UsageLedger(tmp_path / cache.USAGE_FILE, 50_000_000, lambda: clock["now"])
        ledger.observe((1_000, 1_000))
        clock["now"] = NOW - 400 * 86400       # a year and a bit behind
        ledger.observe((2_000, 2_000))
        assert ledger.bytes == 2_000 and ledger.month == "2027-01"
