from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Provider(Enum):
    CLAUDE = "claude"
    ZAI = "zai"
    OLLAMA = "ollama"


class ReadingStatus(Enum):
    CURRENT = "current"
    STALE = "stale"
    OFFLINE = "offline"


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
