"""Rollover evidence, untrusted observations, and bounded waiting contracts."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from usage_dashboard.client.capacity_watch import CapacityWatcher, WatchRequestError, watch_capacity

NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)


def snapshot(at=NOW, *, used=50, reset=None):
    return {
        "schema_version": "agent-capacity-v1",
        "generated_at": at.isoformat(),
        "max_age_seconds": 900,
        "accounts": [
            {
                "account_id": "codex",
                "observed_at": at.isoformat(),
                "freshness": "fresh",
                "windows": [
                    {
                        "name": "weekly",
                        "scope": "account",
                        "is_active": True,
                        "used_percent": used,
                        "state": "exhausted" if used == 100 else "headroom",
                        "resets_at": (reset or NOW + timedelta(minutes=1)).isoformat(),
                    }
                ],
            }
        ],
    }


def watcher():
    return CapacityWatcher("codex", "weekly")


def test_reset_requires_new_observation_and_later_boundary():
    subject = watcher()
    assert subject.observe(snapshot(), NOW)["event"] == "baseline"
    after = NOW + timedelta(minutes=2)
    result = subject.observe(snapshot(after, used=60, reset=after + timedelta(days=7)), after)
    assert result["event"] == "reset"
    assert result["conditions"] == ["reset"]  # A percentage drop is not required.


def test_percentage_drop_is_not_reset():
    subject = watcher()
    subject.observe(snapshot(), NOW)
    later = NOW + timedelta(seconds=10)
    result = subject.observe(snapshot(later, used=1), later)
    assert result["event"] == "changed"
    assert result["conditions"] == []
    assert result["reasons"] == ["usage_decreased_without_reset_evidence"]


def test_changed_deadline_before_boundary_rebases_without_false_rollover():
    subject = watcher()
    subject.observe(snapshot(), NOW)
    later = NOW + timedelta(seconds=10)
    new_boundary = NOW + timedelta(hours=1)
    assert subject.observe(snapshot(later, reset=new_boundary), later)["event"] == "window_changed"
    later = NOW + timedelta(minutes=2)
    result = subject.observe(snapshot(later, used=0, reset=new_boundary), later)
    assert result["conditions"] == []
    later = new_boundary + timedelta(seconds=1)
    result = subject.observe(snapshot(later, reset=later + timedelta(days=7)), later)
    assert result["conditions"] == ["reset"]


def test_elapsed_boundary_preserves_baseline_until_fresh_rollover():
    subject = watcher()
    subject.observe(snapshot(), NOW)
    boundary = NOW + timedelta(minutes=1)
    result = subject.observe(snapshot(), boundary)
    assert result["event"] == "unknown"
    assert result["reasons"] == ["reset_elapsed_without_fresh_window"]
    result = subject.observe(snapshot(boundary, reset=boundary + timedelta(days=7)), boundary)
    assert result["conditions"] == ["reset"]


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("used_percent", True, "invalid_usage"),
        ("used_percent", -1, "invalid_usage"),
        ("used_percent", 101, "invalid_usage"),
        ("used_percent", 10**1000, "invalid_usage"),
        ("used_percent", float("nan"), "invalid_usage"),
        ("used_percent", float("inf"), "invalid_usage"),
        ("used_percent", "100", "invalid_usage"),
        ("state", "unknown", "untrusted_window_state"),
        ("is_active", False, "inactive_window"),
        ("is_active", 1, "inactive_window"),
        ("resets_at", "2026-10-03T12:00:00", "invalid_reset_time"),
        ("resets_at", "bad", "invalid_reset_time"),
    ],
)
def test_untrusted_window_cannot_signal_condition(field, value, reason):
    body = snapshot(used=100)
    body["accounts"][0]["windows"][0][field] = value
    result = watcher().observe(body, NOW)
    assert result["event"] == "unknown"
    assert result["conditions"] == []
    assert result["reasons"] == [reason]


@pytest.mark.parametrize(
    "change,reason",
    [
        (lambda b: b.update(schema_version="future"), "unsupported_schema"),
        (lambda b: b.update(max_age_seconds=True), "invalid_snapshot_metadata"),
        (lambda b: b.update(accounts=None), "invalid_accounts"),
        (lambda b: b.update(accounts=[]), "missing_account"),
        (lambda b: b["accounts"].append(deepcopy(b["accounts"][0])), "ambiguous_account"),
        (lambda b: b["accounts"][0].update(freshness="offline"), "untrusted_account"),
        (lambda b: b["accounts"][0].update(observed_at=None), "invalid_observation_time"),
        (lambda b: b["accounts"][0].update(windows=None), "invalid_windows"),
        (lambda b: b["accounts"][0].update(windows=[]), "missing_window"),
        (
            lambda b: b["accounts"][0]["windows"].append(deepcopy(b["accounts"][0]["windows"][0])),
            "ambiguous_window",
        ),
    ],
)
def test_invalid_structure_stays_unknown(change, reason):
    body = snapshot(used=100)
    change(body)
    assert watcher().observe(body, NOW)["reasons"] == [reason]


def test_freshness_is_independently_checked_against_local_time():
    body = snapshot(used=100)
    assert watcher().observe(body, NOW - timedelta(seconds=1))["reasons"] == ["snapshot_in_future"]
    late = NOW + timedelta(seconds=901)
    assert watcher().observe(body, late)["reasons"] == ["stale_snapshot"]
    body["generated_at"] = late.isoformat()
    assert watcher().observe(body, late)["reasons"] == ["stale_observation"]
    body["accounts"][0]["observed_at"] = (late + timedelta(seconds=1)).isoformat()
    assert watcher().observe(body, late)["reasons"] == ["observation_in_future"]


def test_out_of_order_and_conflicting_observations_preserve_baseline():
    subject = watcher()
    subject.observe(snapshot(), NOW)
    conflict = subject.observe(snapshot(used=100), NOW)
    assert conflict["reasons"] == ["conflicting_observation"]
    old = subject.observe(snapshot(NOW - timedelta(seconds=1), used=100), NOW)
    assert old["reasons"] == ["out_of_order_observation"]
    assert subject.observe(snapshot(), NOW)["event"] == "unchanged"


def test_scope_selection_and_unknown_reset():
    body = snapshot(used=100)
    body["accounts"][0]["windows"][0].update(scope="provider_scoped", resets_at=None)
    assert watcher().observe(body, NOW)["reasons"] == ["missing_window"]
    result = CapacityWatcher("codex", "weekly", "provider_scoped").observe(body, NOW)
    assert result["conditions"] == ["exhausted"]
    assert result["reasons"] == ["reset_time_unknown"]


def test_equivalent_offset_dates_are_same_observation():
    subject = watcher()
    subject.observe(snapshot(), NOW)
    body = snapshot()
    body["accounts"][0]["observed_at"] = "2026-10-02T05:00:00-07:00"
    assert subject.observe(body, NOW)["event"] == "unchanged"


class Clock:
    def __init__(self):
        self.elapsed = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.elapsed

    def now(self):
        return NOW + timedelta(seconds=self.elapsed)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds


def run(fetch, *, clock=None, **kwargs):
    clock = clock or Clock()
    events = []
    status = watch_capacity(
        watcher(),
        fetch,
        events.append,
        monotonic=clock.monotonic,
        utcnow=clock.now,
        sleep=clock.sleep,
        **kwargs,
    )
    return status, events, clock


def test_runner_suppresses_unchanged_and_honors_finite_deadline():
    timeouts = []

    def fetch(timeout):
        timeouts.append(timeout)
        return snapshot()

    status, events, clock = run(fetch, interval_seconds=5, timeout_seconds=12)
    assert status == 2
    assert [e["event"] for e in events] == ["baseline", "timeout"]
    assert clock.sleeps == [5, 5, 2]
    assert timeouts == [12, 7, 2]


def test_runner_marks_recovery_after_repeated_unknown():
    bodies = [snapshot(), snapshot(), snapshot(), snapshot()]
    bodies[1]["accounts"] = []
    bodies[2]["accounts"] = []
    status, events, _ = run(lambda _: bodies.pop(0), interval_seconds=5, timeout_seconds=20)
    assert status == 2
    assert [e["event"] for e in events] == ["baseline", "unknown", "recovered", "timeout"]


@pytest.mark.parametrize("until,expected", [("either", 0), ("exhausted", 0), ("reset", 2)])
def test_runner_condition_selection(until, expected):
    status, events, _ = run(lambda _: snapshot(used=100), until=until, timeout_seconds=1)
    assert status == expected
    assert events[0]["event"] == ("condition_met" if expected == 0 else "baseline")


def test_runner_waits_through_elapsed_boundary_and_stops_on_confirmed_reset():
    clock = Clock()

    def fetch(_):
        return (
            snapshot(clock.now(), reset=NOW + timedelta(days=7))
            if clock.elapsed >= 65
            else snapshot()
        )

    status, events, _ = run(
        fetch, clock=clock, interval_seconds=5, timeout_seconds=80, until="reset"
    )
    assert status == 0
    assert [e["event"] for e in events] == ["baseline", "unknown", "condition_met"]
    assert events[-1]["conditions"] == ["reset"]


def test_late_response_cannot_report_success_after_deadline():
    clock = Clock()

    def fetch(_):
        clock.elapsed = 2
        return snapshot(used=100)

    status, events, _ = run(fetch, clock=clock, timeout_seconds=1)
    assert status == 2
    assert [e["event"] for e in events] == ["timeout"]


@pytest.mark.parametrize(
    "error,status,event",
    [
        (WatchRequestError("SECRET REQUEST DETAILS"), 1, "error"),
        (KeyboardInterrupt(), 130, "interrupted"),
    ],
)
def test_request_failure_and_interrupt_are_terminal_and_sanitized(error, status, event):
    def fetch(_):
        raise error

    actual, events, _ = run(fetch)
    assert actual == status
    assert [e["event"] for e in events] == [event]
    assert "SECRET" not in str(events)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"until": "invalid"},
        {"interval_seconds": 0},
        {"interval_seconds": True},
        {"interval_seconds": 3601},
        {"timeout_seconds": 0},
        {"timeout_seconds": 604801},
    ],
)
def test_invalid_wait_settings_fail_before_fetch(kwargs):
    def fetch(_):
        pytest.fail("invalid configuration made a request")

    with pytest.raises(ValueError):
        run(fetch, **kwargs)
