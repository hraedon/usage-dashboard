"""Umans wallet fetcher (Plan 004).

Polls the wallet summary on the Umans dashboard host and folds it into a
quota-less reading whose ``detail`` carries the formatted balance. Amounts on
the wire are **cents** (floats); the ledger trails real time by ~a minute.

Live-verified 2026-09-09: the documented balance object exposes only
``balanceCents`` / ``funded`` / ``asOf`` — no promo split. Promo/bonus credit
is a documented wallet concept, so plausible promo fields are parsed
tolerantly and shown only when actually present.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from usage_dashboard.server.fetch_types import (
    FetchAuthError,
    FetchError,
    FetchRateLimitError,
    dump_json,
)
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus

# The wallet API lives on the dashboard host, not the gateway
# (api.code.umans.ai), and takes the same gateway key as Bearer.
_UMANS_WALLET_URL = "https://app.umans.ai/api/v1/wallet/summary"
_TIMEOUT = 30.0

# A wallet with no wallet behind it (an archived-plan key) 404s with this
# coded error. That is a documented steady state (umans usage renders its
# plan view on it), not a failure to retry.
_WALLET_NOT_FOUND = "wallet_not_found"

# Field names checked for promo credit, in preference order. None are
# documented or observed live today; when one appears the corner line grows
# its ", promo: $X" tail without a format change.
_PROMO_KEYS = ("promoCents", "promo_cents", "promoBalanceCents")


def _money(cents: float) -> str:
    """Format a cents amount the way the wallet tab shows it."""
    return f"${cents / 100.0:,.2f}"


def _as_cents(value: object) -> float | None:
    """Coerce a JSON number to finite non-negative cents, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    cents = float(value)
    if cents != cents or cents in (float("inf"), float("-inf")) or cents < 0:
        return None
    return cents


def _find_promo_cents(payload: dict[str, Any], balance: dict[str, Any]) -> float | None:
    """Best-effort promo lookup across the plausible containers."""
    for source in (balance, payload):
        for key in _PROMO_KEYS:
            cents = _as_cents(source.get(key))
            if cents is not None and cents > 0:
                return cents
    return None


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse Retry-After: delay-seconds, or an HTTP-date (RFC 7231 §7.1.3).

    A date in the past (or an unparseable value) yields None so the scheduler
    falls back to its rate-limit default rather than a zero backoff.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if retry_at is None:
        return None
    now = datetime.now(timezone.utc)
    retry_at = (
        retry_at.replace(tzinfo=timezone.utc)
        if retry_at.tzinfo is None
        else retry_at.astimezone(timezone.utc)
    )
    delta = (retry_at - now).total_seconds()
    # A date in the past carries no usable hint; None lets the scheduler fall
    # back to its rate-limit default instead of a zero backoff.
    return delta if delta > 0 else None


def fetch_umans_wallet(api_key: str) -> Reading:
    """Fetch the wallet summary as a quota-less reading.

    ``detail`` is the display text for the Pi's corner line (without the
    "Umans: " prefix the layout adds): ``$16.32``, ``$16.32, promo: $7.14``,
    or ``no wallet`` for an archived-plan key.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.get(_UMANS_WALLET_URL, headers=headers)
            if response.status_code == 404:
                # Distinguish the coded archived-plan state from any other
                # 404 before raise_for_status collapses them. A non-JSON body
                # (a proxy page, say) is not the coded state — fall through.
                try:
                    body = response.json() if response.content else {}
                except ValueError:
                    body = None
                if isinstance(body, dict) and body.get("code") == _WALLET_NOT_FOUND:
                    return _reading("no wallet")
            response.raise_for_status()
            payload = response.json()
            dump_json("umans_wallet_raw.json", payload)
            if not isinstance(payload, dict):
                raise ValueError("wallet summary is not an object")
            balance = payload.get("balance")
            if not isinstance(balance, dict):
                raise ValueError("wallet summary missing balance object")
            balance_cents = _as_cents(balance.get("balanceCents"))
            if balance_cents is None:
                raise ValueError("wallet balance missing balanceCents")
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in (401, 403):
            raise FetchAuthError(
                f"Umans wallet request rejected: HTTP {status}"
            ) from exc
        if status == 429:
            raise FetchRateLimitError(
                "Umans wallet rate limited: HTTP 429",
                retry_after_seconds=_retry_after_seconds(exc.response),
            ) from exc
        raise FetchError(f"Umans wallet request failed: HTTP {status}") from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"Umans wallet request failed: {type(exc).__name__}") from exc
    except (ValueError, TypeError) as exc:
        raise FetchError(f"Umans wallet response parse error: {exc}") from exc

    detail = _money(balance_cents)
    promo_cents = _find_promo_cents(payload, balance)
    if promo_cents is not None:
        detail += f", promo: {_money(promo_cents)}"
    return _reading(detail)


def _reading(detail: str) -> Reading:
    return Reading(
        provider=Provider.UMANS,
        status=ReadingStatus.CURRENT,
        session_percent=None,
        session_resets_at=None,
        weekly_percent=None,
        weekly_resets_at=None,
        fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
        stale=False,
        detail=detail,
    )
