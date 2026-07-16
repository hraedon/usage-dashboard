from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Provider(Enum):
    CLAUDE = "claude"
    # A second, optional Claude account (e.g. a work login) with its own OAuth
    # pair. It reuses the whole Claude fetch/refresh/storage path; the clients
    # merge it into the Claude tile as a second, muted set of bars. Absent
    # unless its own tokens are configured.
    CLAUDE_WORK = "claude_work"
    ZAI = "zai"
    OLLAMA = "ollama"
    CODEX = "codex"
    UMANS = "umans"


# Claude-family providers that share the OAuth fetch/refresh machinery, mapped
# to their token-store namespace. The client renders them as one "Claude" tile.
CLAUDE_PROVIDERS: dict[Provider, str] = {
    Provider.CLAUDE: "claude",
    Provider.CLAUDE_WORK: "claude_work",
}


class ReadingStatus(Enum):
    CURRENT = "current"
    STALE = "stale"
    OFFLINE = "offline"


# Throttle severity for quota-less providers (umans), which have no percentage
# to colour by. "low" = deprioritised routing (e.g. exceeded the concurrency
# threshold); "low_interactivity" = umans' service_mode penalty for a heavy
# trailing day — requests queue behind interactive sessions until
# service_mode.resets_at (a distinct, softer state than the priority/boxed
# ladder; observed live 2026-07-14, sluice
# samples/service-mode-capture-2026-07-14.md); "rate_limited" = a limit hit set
# boxed_until with priority.reason="rate_limited" — the account KEEPS SERVING
# at low priority for the window (proven live 2026-07-03, sluice
# docs/wi-024-429-capture-2026-07-03.md); "boxed" = penalty box, the account
# is locked for the window. Worse states win, so a provider that is both low
# and boxed reports "boxed"; an unexpired boxed_until without the known-soft
# rate_limited reason is always "boxed" (fail safe).
THROTTLE_NONE = "none"
THROTTLE_LOW = "low"
THROTTLE_LOW_INTERACTIVITY = "low_interactivity"
THROTTLE_RATE_LIMITED = "rate_limited"
THROTTLE_BOXED = "boxed"

# Volume alert for quota-less providers: how close the trailing-window token
# total is to the (opaque, empirically-guessed) heavy-usage threshold that
# triggers low-interactivity mode. Computed server-side from configurable
# thresholds (UMANS_TOKENS_WARN/UMANS_TOKENS_CRIT) so the display isn't locked
# into today's guesses. Advisory only — throttle states always outrank it.
ALERT_NONE = "none"
ALERT_WARN = "warn"
ALERT_CRIT = "crit"


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """A single model/tool's share of a provider's usage.

    For Ollama this is parsed from the per-model segment buttons in the weekly
    usage bar (share = segment width %, requests = data-requests). For z.ai it
    comes from the TIME_LIMIT API-tools quota's usageDetails (share = share of
    used calls, requests = call count).
    """

    name: str
    requests: int
    share_percent: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "requests": self.requests,
            "share_percent": self.share_percent,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelUsage:
        return cls(
            name=data["name"],
            requests=data["requests"],
            share_percent=data["share_percent"],
        )


@dataclass(frozen=True, slots=True)
class ScopedLimit:
    """A usage window scoped to a single model (or surface), reported by the
    Claude ``/api/oauth/usage`` endpoint in its ``limits[]`` array.

    The top-level ``five_hour``/``seven_day`` blocks are all-model aggregates;
    ``limits[]`` additionally carries ``weekly_scoped`` entries whose ``scope``
    names a specific model (e.g. ``{"model": {"display_name": "Fable"}}``). This
    is the only place per-model Claude usage is exposed, so a scoped limit is
    rendered as its own extra bar rather than folded into the aggregate windows.

    ``is_active`` reflects the endpoint's flag for whether this limit is the
    currently-binding constraint on the plan (a scoped limit can be present but
    not active).
    """

    name: str
    percent: float | None
    resets_at: datetime | None
    is_active: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "percent": self.percent,
            "resets_at": _format_dt(self.resets_at),
            "is_active": self.is_active,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScopedLimit:
        return cls(
            name=data["name"],
            percent=data["percent"],
            resets_at=_parse_dt(data["resets_at"]),
            is_active=data.get("is_active", False),
        )


def _format_dt(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    utc_dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def _parse_required_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


@dataclass(frozen=True, slots=True)
class Reading:
    provider: Provider
    status: ReadingStatus
    session_percent: float | None
    session_resets_at: datetime | None
    weekly_percent: float | None
    weekly_resets_at: datetime | None
    fetched_at: datetime
    stale: bool
    detail: str | None = None
    models: list[ModelUsage] | None = None
    # Throttle severity (THROTTLE_NONE/LOW/BOXED). Quota-less providers use this
    # as their only severity signal, since they have no percentage to colour by.
    throttle: str = THROTTLE_NONE
    # Volume alert (ALERT_NONE/WARN/CRIT): trailing-window token total vs the
    # configured heavy-usage thresholds. Advisory colour cue; throttle wins.
    alert: str = ALERT_NONE
    # Extra per-model usage windows (Claude ``limits[]`` weekly_scoped entries,
    # e.g. Fable). Rendered as additional bars; None/absent for providers that
    # don't report scoped limits.
    scoped_limits: list[ScopedLimit] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider.value,
            "status": self.status.value,
            "session_percent": self.session_percent,
            "session_resets_at": _format_dt(self.session_resets_at),
            "weekly_percent": self.weekly_percent,
            "weekly_resets_at": _format_dt(self.weekly_resets_at),
            "fetched_at": _format_dt(self.fetched_at),
            "stale": self.stale,
            "detail": self.detail,
            "models": [m.to_dict() for m in self.models] if self.models else None,
            "throttle": self.throttle,
            "alert": self.alert,
            "scoped_limits": (
                [s.to_dict() for s in self.scoped_limits] if self.scoped_limits else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Reading:
        return cls(
            provider=Provider(data["provider"]),
            status=ReadingStatus(data["status"]),
            session_percent=data["session_percent"],
            session_resets_at=_parse_dt(data["session_resets_at"]),
            weekly_percent=data["weekly_percent"],
            weekly_resets_at=_parse_dt(data["weekly_resets_at"]),
            fetched_at=_parse_required_dt(data["fetched_at"]),
            stale=data["stale"],
            detail=data.get("detail"),
            models=(
                [ModelUsage.from_dict(m) for m in data["models"]]
                if data.get("models")
                else None
            ),
            throttle=data.get("throttle", THROTTLE_NONE) or THROTTLE_NONE,
            alert=data.get("alert", ALERT_NONE) or ALERT_NONE,
            scoped_limits=(
                [ScopedLimit.from_dict(s) for s in data["scoped_limits"]]
                if data.get("scoped_limits")
                else None
            ),
        )


def make_offline_reading(provider: Provider, fetched_at: datetime) -> Reading:
    return Reading(
        provider=provider,
        status=ReadingStatus.OFFLINE,
        session_percent=None,
        session_resets_at=None,
        weekly_percent=None,
        weekly_resets_at=None,
        fetched_at=fetched_at,
        stale=True,
    )


def make_stale_reading(reading: Reading) -> Reading:
    return replace(reading, stale=True, status=ReadingStatus.STALE)
