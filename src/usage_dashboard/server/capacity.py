"""Read-only agent projection of cached usage; no dispatch or provider I/O."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Literal

from usage_dashboard.shared.models import Provider, Reading, ReadingStatus

Assessment = Literal["known_blocked", "no_known_quota_block", "unknown"]
WindowState = Literal["exhausted", "headroom", "unknown"]
Freshness = Literal["fresh", "stale", "offline", "missing", "invalid"]

# Standard scheduler idle ceiling is 30 min; allow one 5 min floor interval
# for scheduling/fetch latency. Callers can require a stricter observation age.
DEFAULT_MAX_AGE_SECONDS = 2100


@dataclass(frozen=True)
class CapacityWindow:
    name: str
    scope: Literal["account", "provider_scoped"]
    is_active: bool
    used_percent: float | None
    remaining_percent: float | None
    resets_at: str | None
    state: WindowState
    reasons: list[str]


@dataclass(frozen=True)
class AccountCapacity:
    account_id: str
    observed_at: str | None
    age_seconds: float | None
    freshness: Freshness
    assessment: Assessment
    reasons: list[str]
    windows: list[CapacityWindow]
    throttle: str | None
    alert: str | None


@dataclass(frozen=True)
class CapacitySnapshot:
    schema_version: Literal["agent-capacity-v1"]
    generated_at: str
    max_age_seconds: int
    accounts: list[AccountCapacity]
    assessment_scope: Literal["reported_limits_only"] = "reported_limits_only"
    job_fit: Literal["not_assessed"] = "not_assessed"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(
        timezone.utc
    )


def timestamp(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _window(
    name: str,
    percent: float | None,
    reset: datetime | None,
    now: datetime,
    *,
    scoped: bool = False,
    active: bool = True,
) -> CapacityWindow:
    reasons: list[str] = []
    valid = (
        isinstance(percent, (int, float))
        and not isinstance(percent, bool)
        and math.isfinite(percent)
        and 0 <= percent <= 100
    )
    used = float(percent) if valid and percent is not None else None
    if used is None:
        reasons.append("missing_usage" if percent is None else "invalid_usage")
    if reset is not None and _utc(reset) <= _utc(now):
        reasons.append("reset_elapsed")
    state: WindowState = "unknown"
    if not reasons and used is not None:
        state = "exhausted" if used >= 100 else "headroom"
    return CapacityWindow(
        name=name,
        scope="provider_scoped" if scoped else "account",
        is_active=active,
        used_percent=used,
        remaining_percent=None if used is None else 100 - used,
        resets_at=timestamp(reset) if reset is not None else None,
        state=state,
        reasons=reasons,
    )


def account_capacity(
    provider: Provider, reading: Reading | None, *, now: datetime, max_age_seconds: int
) -> AccountCapacity:
    if reading is None:
        return AccountCapacity(
            provider.value, None, None, "missing", "unknown", ["no_reading"], [], None, None
        )
    age = (_utc(now) - _utc(reading.fetched_at)).total_seconds()
    freshness: Freshness = "fresh"
    reasons: list[str] = []
    if age < 0:
        freshness = "invalid"
        reasons.append("observation_in_future")
    elif reading.status == ReadingStatus.OFFLINE:
        freshness = "offline"
        reasons.append("provider_offline")
    elif reading.stale or reading.status == ReadingStatus.STALE or age > max_age_seconds:
        freshness = "stale"
        reasons.append("stale_observation")

    windows: list[CapacityWindow] = []
    for name, percent, reset in (
        ("session", reading.session_percent, reading.session_resets_at),
        ("weekly", reading.weekly_percent, reading.weekly_resets_at),
    ):
        # Omitted windows are not fabricated: Codex can report weekly only.
        if percent is not None or reset is not None:
            windows.append(_window(name, percent, reset, now))
    for limit in reading.scoped_limits or []:
        # The legacy reading schema also stores OpenCode's third account-wide
        # window here. It is an aggregate quota, not an inactive model scope.
        monthly_account_limit = provider == Provider.OPENCODE and limit.name == "Monthly"
        windows.append(_window(
            limit.name, limit.percent, limit.resets_at, now,
            scoped=not monthly_account_limit,
            active=monthly_account_limit or limit.is_active,
        ))

    if freshness != "fresh":
        windows = [
            replace(w, state="unknown", reasons=[*w.reasons, "untrusted_observation"])
            for w in windows
        ]

    assessment: Assessment = "unknown"
    if freshness == "fresh":
        if reading.throttle == "boxed":
            assessment = "known_blocked"
            reasons.append("provider_boxed")
        elif any(w.state == "exhausted" and w.is_active for w in windows):
            assessment = "known_blocked"
            reasons.append("quota_exhausted")
        elif any(w.state == "unknown" for w in windows):
            reasons.append("uncertain_window")
        elif any(w.state == "exhausted" for w in windows):
            reasons.append("scope_selection_required")
        elif reading.throttle != "none":
            # Soft throttles do not prove a hard block or available capacity.
            reasons.append("provider_throttled")
        elif not windows:
            reasons.append("no_reported_limits")
        else:
            assessment = "no_known_quota_block"
            reasons.append("reported_limits_have_headroom")

    return AccountCapacity(
        account_id=provider.value,
        observed_at=timestamp(reading.fetched_at),
        age_seconds=round(age, 3),
        freshness=freshness,
        assessment=assessment,
        reasons=reasons,
        windows=windows,
        throttle=reading.throttle,
        alert=reading.alert,
    )
