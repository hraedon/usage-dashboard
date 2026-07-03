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
# threshold); "rate_limited" = a limit hit set boxed_until with
# priority.reason="rate_limited" — the account KEEPS SERVING at low priority
# for the window (proven live 2026-07-03, sluice
# docs/wi-024-429-capture-2026-07-03.md); "boxed" = penalty box, the account
# is locked for the window. Worse states win, so a provider that is both low
# and boxed reports "boxed"; an unexpired boxed_until without the known-soft
# rate_limited reason is always "boxed" (fail safe).
THROTTLE_NONE = "none"
THROTTLE_LOW = "low"
THROTTLE_RATE_LIMITED = "rate_limited"
THROTTLE_BOXED = "boxed"


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
