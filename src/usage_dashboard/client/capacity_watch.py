"""Read-only observation of a selected quota window; no inference about job fit."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

Scope = Literal["account", "provider_scoped"]
Condition = Literal["exhausted", "reset", "either"]


class WatchRequestError(Exception):
    """Sanitized transport/configuration error, without request or response data."""


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _date(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class _Observation:
    observed_at: datetime
    used: float
    reset: datetime | None

    @property
    def signature(self) -> tuple[float, datetime | None]:
        return self.used, self.reset


class CapacityWatcher:
    """Track only fresh, active observations of one explicitly selected window."""

    def __init__(self, account: str, window: str, scope: Scope = "account") -> None:
        self.account = account
        self.window = window
        self.scope = scope
        self.previous: _Observation | None = None

    def envelope(self, event: str, now: datetime, **fields: object) -> dict[str, object]:
        return {
            "schema_version": "capacity-watch-v1",
            "event": event,
            "checked_at": _timestamp(now),
            "account_id": self.account,
            "window": self.window,
            "scope": self.scope,
            **fields,
        }

    def _read(
        self,
        snapshot: dict[str, Any],
        now: datetime,
    ) -> tuple[_Observation | None, str | None]:
        if snapshot.get("schema_version") != "agent-capacity-v1":
            return None, "unsupported_schema"
        generated = _date(snapshot.get("generated_at"))
        max_age = snapshot.get("max_age_seconds")
        if generated is None or type(max_age) is not int or not 1 <= max_age <= 86400:
            return None, "invalid_snapshot_metadata"
        if generated > now:
            return None, "snapshot_in_future"
        if (now - generated).total_seconds() > max_age:
            return None, "stale_snapshot"
        accounts = snapshot.get("accounts")
        if not isinstance(accounts, list):
            return None, "invalid_accounts"
        matches = [
            item
            for item in accounts
            if isinstance(item, dict) and item.get("account_id") == self.account
        ]
        if len(matches) != 1:
            return None, "missing_account" if not matches else "ambiguous_account"
        account = matches[0]
        if account.get("freshness") != "fresh":
            return None, "untrusted_account"
        observed = _date(account.get("observed_at"))
        if observed is None:
            return None, "invalid_observation_time"
        if observed > generated or observed > now:
            return None, "observation_in_future"
        if (now - observed).total_seconds() > max_age:
            return None, "stale_observation"
        windows = account.get("windows")
        if not isinstance(windows, list):
            return None, "invalid_windows"
        selected = [
            item
            for item in windows
            if isinstance(item, dict)
            and item.get("name") == self.window
            and item.get("scope") == self.scope
        ]
        if len(selected) != 1:
            return None, "missing_window" if not selected else "ambiguous_window"
        window = selected[0]
        if window.get("is_active") is not True:
            return None, "inactive_window"
        used = window.get("used_percent")
        if (
            not isinstance(used, (int, float))
            or isinstance(used, bool)
            or not 0 <= used <= 100
            or not math.isfinite(used)
        ):
            return None, "invalid_usage"
        expected_state = "exhausted" if used == 100 else "headroom"
        if window.get("state") != expected_state:
            return None, "untrusted_window_state"
        raw_reset = window.get("resets_at")
        reset = _date(raw_reset) if raw_reset is not None else None
        if raw_reset is not None and reset is None:
            return None, "invalid_reset_time"
        if reset is not None and reset <= now:
            return None, "reset_elapsed_without_fresh_window"
        return _Observation(observed, float(used), reset), None

    def observe(self, snapshot: dict[str, Any], now: datetime) -> dict[str, object]:
        current, reason = self._read(snapshot, now)
        if current is None:
            return self.envelope("unknown", now, reasons=[reason], conditions=[])
        previous = self.previous
        if previous is not None:
            if current.observed_at < previous.observed_at:
                return self.envelope(
                    "unknown", now, reasons=["out_of_order_observation"], conditions=[]
                )
            if (
                current.observed_at == previous.observed_at
                and current.signature != previous.signature
            ):
                return self.envelope(
                    "unknown", now, reasons=["conflicting_observation"], conditions=[]
                )
        event = "baseline" if previous is None else "unchanged"
        reasons: list[str] = []
        conditions = ["exhausted"] if current.used == 100 else []
        if previous is not None and current.signature != previous.signature:
            event = "changed"
            if current.reset != previous.reset:
                if (
                    previous.reset is not None
                    and current.reset is not None
                    and current.reset > previous.reset
                    and current.observed_at >= previous.reset
                    and current.observed_at > previous.observed_at
                ):
                    event = "reset"
                    conditions.append("reset")
                    reasons.append("new_observation_after_prior_reset")
                else:
                    event = "window_changed"
                    reasons.append("reset_boundary_changed_without_rollover_evidence")
            elif current.used < previous.used:
                reasons.append("usage_decreased_without_reset_evidence")
        if current.reset is None:
            reasons.append("reset_time_unknown")
        self.previous = current
        return self.envelope(
            event,
            now,
            observed_at=_timestamp(current.observed_at),
            used_percent=current.used,
            remaining_percent=100 - current.used,
            resets_at=_timestamp(current.reset) if current.reset is not None else None,
            previous_resets_at=_timestamp(previous.reset)
            if previous is not None and previous.reset is not None
            else None,
            conditions=conditions,
            reasons=reasons,
        )


def watch_capacity(
    watcher: CapacityWatcher,
    fetch: Callable[[float], dict[str, Any]],
    emit: Callable[[dict[str, object]], None],
    *,
    until: Condition = "either",
    interval_seconds: int = 60,
    timeout_seconds: int = 3600,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> int:
    """Stream meaningful changes until a condition, finite deadline, or error.

    ``fetch`` receives the maximum timeout for its next request. It must raise
    WatchRequestError containing only a safe diagnostic. HTTP transport timeout
    semantics apply; the deadline is checked again after the request completes.
    """
    if until not in {"either", "exhausted", "reset"}:
        raise ValueError("until must be exhausted, reset or either")
    if type(interval_seconds) is not int or not 5 <= interval_seconds <= 3600:
        raise ValueError("interval_seconds must be an integer from 5 to 3600")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 604800:
        raise ValueError("timeout_seconds must be an integer from 1 to 604800")
    started = monotonic()
    deadline = started + timeout_seconds
    previous_unknown: tuple[object, ...] | None = None
    try:
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                emit(watcher.envelope("timeout", utcnow(), until=until))
                return 2
            snapshot = fetch(min(15.0, remaining))
            if monotonic() >= deadline:
                emit(watcher.envelope("timeout", utcnow(), until=until))
                return 2
            update = watcher.observe(snapshot, utcnow())
            conditions = update["conditions"]
            matched = isinstance(conditions, list) and (
                bool(conditions) if until == "either" else until in conditions
            )
            if matched:
                emit({**update, "event": "condition_met", "until": until})
                return 0
            if update["event"] == "unknown":
                reasons = update["reasons"]
                signature = tuple(reasons) if isinstance(reasons, list) else ()
                if signature != previous_unknown:
                    emit(update)
                previous_unknown = signature
            else:
                if update["event"] != "unchanged" or previous_unknown is not None:
                    emit(
                        {**update, "event": "recovered"}
                        if update["event"] == "unchanged"
                        else update
                    )
                previous_unknown = None
            remaining = deadline - monotonic()
            if remaining > 0:
                sleep(min(interval_seconds, remaining))
    except WatchRequestError:
        # No exception text: a caller may accidentally include credential or
        # endpoint details even though the adapter contract forbids it.
        emit(watcher.envelope("error", utcnow(), reasons=["capacity_request_failed"]))
        return 1
    except KeyboardInterrupt:
        emit(watcher.envelope("interrupted", utcnow()))
        return 130
