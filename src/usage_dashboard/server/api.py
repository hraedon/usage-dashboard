from __future__ import annotations

import asyncio
import hmac
import html
import math
import threading
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from usage_dashboard.server.db import Database
from usage_dashboard.server.schedule_config import ScheduleConfig
from usage_dashboard.server.scheduler import FetchScheduler
from usage_dashboard.shared.models import (
    ALERT_CRIT,
    ALERT_WARN,
    Provider,
    Reading,
    ReadingStatus,
    make_offline_reading,
)
from usage_dashboard.shared.offpeak import qwen_peak_countdown, zai_is_offpeak

_bearer_scheme = HTTPBearer(auto_error=False)

# Every authenticated route is served under this prefix (Plan 003 WP-2). The
# unauthenticated routes (`/`, `/dashboard`, `/health`) deliberately stay off
# `/api` entirely — that is what lets the external ingress route one `/api`
# prefix instead of a hand-maintained path list that has silently 404'd the Pi
# fleet three times (WI-024).
API_V1_PREFIX = "/api/v1"

# The pre-prefix paths keep working. The server rolls on an image rebuild while
# the Pis roll on their own 15-minute `update.sh` timer, so a flag-day rename
# would 404 the whole fleet until every unit caught up. Both path sets are
# served from ONE router mounted twice, so they cannot drift apart. Retiring
# these is WP-4, gated on both units being confirmed on a WP-2 client.
LEGACY_ALIAS_PREFIX = ""

# --- Exposure declaration (Plan 003 WP-3, open question 1) -------------------
#
# Whether a route is reachable from the public host is DECLARED, never inferred.
# Inferring it from "is it authenticated" is default-allow: with a single `/api`
# Prefix on the external ingress, every new authenticated route would be
# internet-reachable the moment it exists. That is how `/history` got exposed on
# 2026-08-07 without anyone deciding to.
#
# Spread these into the route decorator. The contract test requires EVERY
# authenticated route to carry one — an undeclared route fails the build rather
# than defaulting either way, so adding a route forces the decision.
#
#   @api.get("/readings", **EXTERNAL)
#   @api.get("/admin/reset", **INTERNAL_ONLY)
#
# INTERNAL_ONLY routes must be mounted off `/api` (see INTERNAL_V1_PREFIX) so
# the single external `/api` rule cannot reach them; the guard enforces that a
# declared-internal route is NOT covered by the external ingress.
EXPOSURE_KEY = "x-exposure"
EXPOSURE_EXTERNAL = "external"
EXPOSURE_INTERNAL_ONLY = "internal-only"

EXTERNAL: dict[str, Any] = {"openapi_extra": {EXPOSURE_KEY: EXPOSURE_EXTERNAL}}
INTERNAL_ONLY: dict[str, Any] = {"openapi_extra": {EXPOSURE_KEY: EXPOSURE_INTERNAL_ONLY}}

# Reserved for authenticated-but-internal-only routes (an admin or debug
# endpoint). Deliberately NOT under /api, which the external ingress routes
# wholesale. Empty today; the machinery exists so the first such route cannot
# be exposed by accident.
INTERNAL_V1_PREFIX = "/internal/v1"


def route_exposure(route: Any) -> str | None:
    """Declared exposure for *route*, or None if it never declared one."""
    extra = getattr(route, "openapi_extra", None) or {}
    value = extra.get(EXPOSURE_KEY)
    return value if isinstance(value, str) else None

# Upper bound for /history windows — matches the default 7-day retention
# (RETENTION_DAYS). Larger windows are accepted up to this cap; rows older
# than retention simply won't exist.
_MAX_HISTORY_HOURS = 24 * 7

# Minimum gap between client-triggered refreshes (POST /refresh). The touch GUI
# polls every 5-30m on its own; a forced refresh beyond this pace would hammer
# the provider APIs into their own 429 limits.
_REFRESH_MIN_INTERVAL_SECONDS = 60.0

# Same thresholds as the Pi client's bar_color (client/format.py)
_CSS_GREEN = "#22c55e"
_CSS_ORANGE = "#f97316"
_CSS_RED = "#ef4444"
_CSS_GRAY = "#969696"


def _bar_color_css(percent: float | None) -> str:
    if percent is None:
        return _CSS_GRAY
    if percent >= 85:
        return _CSS_RED
    if percent >= 75:
        return _CSS_ORANGE
    return _CSS_GREEN


def _alert_color_css(alert: str) -> str:
    """Alert severity -> detail-line colour (warn/orange, crit/red)."""
    if alert == ALERT_CRIT:
        return _CSS_RED
    if alert == ALERT_WARN:
        return _CSS_ORANGE
    return _CSS_GRAY


def _countdown_text(resets_at: datetime | None, now: datetime) -> str:
    if resets_at is None:
        return ""
    total_seconds = int((resets_at - now).total_seconds())
    if total_seconds <= 0:
        return "resets 0m"
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    if days > 0:
        return f"resets {days}d {hours}h"
    return f"resets {hours}h {minutes}m"


def _countdown_short(total_seconds: float) -> str:
    """Abbreviated countdown for window boundaries: 204 -> '3h 24m'."""
    seconds = max(0, int(total_seconds))
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{max(1, minutes)}m"


def _status_badge(reading: Reading) -> str:
    if reading.status == ReadingStatus.OFFLINE:
        return ' <span class="badge">offline</span>'
    if reading.status == ReadingStatus.STALE or reading.stale:
        return ' <span class="badge">stale</span>'
    return ""


def _bar_row(label: str, percent: float | None, resets_at: datetime | None, now: datetime) -> str:
    color = _bar_color_css(percent)
    width = min(percent, 100.0) if percent is not None else 0.0
    pct_text = f"{percent:.0f}%" if percent is not None else "N/A"
    countdown = _countdown_text(resets_at, now)
    return (
        f'<div class="row"><span class="label">{label}</span>'
        f'<span class="track"><span class="fill" style="width:{width:.0f}%;'
        f'background:{color}"></span></span>'
        f'<span class="pct">{pct_text}</span></div>'
        f'<div class="resets">{countdown}</div>'
    )


def _account_rows(reading: Reading, now: datetime, label: str = "") -> str:
    prefix = f"{label} " if label else ""
    rows = ""
    if reading.session_percent is not None:
        rows += _bar_row(
            f"{prefix}Session", reading.session_percent, reading.session_resets_at, now
        )
    if reading.weekly_percent is not None:
        rows += _bar_row(
            f"{prefix}Weekly", reading.weekly_percent, reading.weekly_resets_at, now
        )
    for sl in reading.scoped_limits or []:
        rows += _bar_row(f"{prefix}{sl.name}", sl.percent, sl.resets_at, now)
    return rows


def _render_dashboard_html(readings: list[Reading], now: datetime) -> str:
    by_provider = {r.provider: r for r in readings}
    work = by_provider.get(Provider.CLAUDE_WORK)
    cards: list[str] = []
    for reading in readings:
        # The work Claude account folds into the Claude card, not its own.
        if reading.provider == Provider.CLAUDE_WORK:
            continue
        name = html.escape(reading.provider.value.upper()) + _status_badge(reading)
        # z.ai's plan discounts off-peak use (peak = Mon–Fri 14:00–18:00
        # Singapore time): tint the card header green off-peak / orange peak so
        # "use it now vs wait it out" is glanceable from across the room.
        header_style = ""
        if reading.provider == Provider.ZAI:
            color = _CSS_GREEN if zai_is_offpeak(now) else _CSS_ORANGE
            header_style = f' style="color:{color}"'
        if reading.provider == Provider.CLAUDE and work is not None:
            body = _account_rows(reading, now, "me") + _account_rows(work, now, "work")
        else:
            body = _account_rows(reading, now)
        if reading.detail:
            # Detail lines render under the percentage bars (z.ai's weekly
            # token total) or as the whole card body (quota-less providers).
            # Coloured by the volume alert so "close to the wall" is glanceable.
            detail_color = _alert_color_css(reading.alert)
            body += (
                f'<div class="detail" style="color:{detail_color}">'
                f"{html.escape(reading.detail)}</div>"
            )
        cards.append(f'<section class="card"><h2{header_style}>{name}</h2>{body}</section>')

    # Display-only QWEN tag: no data source, just whether we're inside the Qwen
    # token plan's off-peak window (22:00–08:00 UTC+8), when credits consume
    # much less — with a countdown to the next boundary. Replaces the retired
    # umans card.
    qwen = qwen_peak_countdown(now)
    qwen_color = _CSS_GREEN if not qwen.in_peak else _CSS_ORANGE
    qwen_label = (
        f"peak in {_countdown_short(qwen.seconds_to_boundary)}"
        if not qwen.in_peak
        else f"peak ends in {_countdown_short(qwen.seconds_to_boundary)}"
    )
    cards.append(
        f'<section class="card"><h2 style="color:{qwen_color}">QWEN</h2>'
        f'<div class="detail" style="color:{qwen_color}">{qwen_label}</div></section>'
    )

    fetched = max((r.fetched_at for r in readings), default=now)
    footer = (
        f"fetched {fetched.strftime('%Y-%m-%d %H:%M:%S')} UTC "
        "&middot; refreshes every 5&ndash;30m (adaptive)"
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>AI Usage</title>
<style>
:root {{ --maxw: 1100px; }}
* {{ box-sizing: border-box; }}
body {{ background:#000; color:#fff; font-family:-apple-system,system-ui,sans-serif;
  margin:0; padding:12px; }}
header, footer, .grid {{ max-width:var(--maxw); margin-inline:auto; }}
header h1 {{ margin:4px 4px 12px; font-size:1.1rem; font-weight:600;
  letter-spacing:0.06em; color:#ddd; }}
/* Fluid grid: 1 column on a phone, 2 on a tablet, up to 4 on a desktop,
   driven by the card min width — no per-device breakpoints needed. */
.grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr));
  gap:12px; align-items:start; }}
.card {{ background:#111; border-radius:12px; padding:14px 16px; }}
h2 {{ margin:0 0 10px; font-size:1.05rem; letter-spacing:0.04em; }}
.badge {{ font-size:0.7rem; color:#eab308; border:1px solid #eab308;
  border-radius:6px; padding:1px 6px; vertical-align:middle; }}
.row {{ display:flex; align-items:center; gap:10px; }}
.label {{ width:96px; font-size:0.85rem; color:#ccc; }}
.track {{ flex:1; height:12px; background:#323232; border-radius:6px; overflow:hidden;
  display:block; }}
.fill {{ display:block; height:100%; }}
.pct {{ width:44px; text-align:right; font-variant-numeric:tabular-nums; }}
.resets {{ margin:2px 0 8px 106px; font-size:0.75rem; color:#969696; }}
.detail {{ font-size:1.0rem; color:#ccc; font-variant-numeric:tabular-nums; }}
footer {{ text-align:center; color:#555; font-size:0.7rem; margin-top:12px; }}
</style>
</head>
<body>
<header><h1>AI Usage</h1></header>
<main class="grid">
{"".join(cards)}
</main>
<footer>{footer}</footer>
</body>
</html>"""


def _make_auth_dependency(
    api_key: str,
) -> Callable[..., Any]:
    async def verify_bearer(
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    ) -> str:
        if credentials is None:
            raise HTTPException(status_code=401)
        if not hmac.compare_digest(credentials.credentials, api_key):
            raise HTTPException(status_code=401)
        return credentials.credentials

    return verify_bearer


def create_app(
    api_key: str,
    db: Database,
    configured_providers: Iterable[Provider] | None = None,
    schedule_config: ScheduleConfig | None = None,
    scheduler: FetchScheduler | None = None,
) -> FastAPI:
    app = FastAPI()
    auth = _make_auth_dependency(api_key)
    # Authenticated routes live on this router so they can be mounted at both
    # /api/v1 and the legacy root paths from a single definition.
    api = APIRouter()

    # Only report providers that are actually configured. A provider that was
    # never configured is omitted entirely rather than fabricated as "offline",
    # so a real outage (configured but not reporting) is distinguishable from
    # an absent config (WI-003). ``None`` means "assume all providers".
    providers: list[Provider] = (
        list(configured_providers)
        if configured_providers is not None
        else list(Provider)
    )

    def _reported_readings() -> list[Reading]:
        readings: dict[Provider, Reading] = db.get_latest_readings()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        return [
            readings.get(provider) or make_offline_reading(provider, now)
            for provider in providers
        ]

    @api.get("/readings", **EXTERNAL)
    async def get_readings(
        _user: str = Depends(auth),
    ) -> list[dict[str, Any]]:
        return [reading.to_dict() for reading in _reported_readings()]

    # On-demand refresh (WI-012): the touch GUI posts here to force an
    # immediate collection cycle instead of waiting out the idle ladder.
    # Rate-limit state lives in the closure so each app instance (and each
    # test) gets an independent limiter; ``scheduler is None`` means the app
    # was built without a scheduler (tests, or a config-only server) and the
    # endpoint reports it as unavailable rather than crashing.
    refresh_lock = threading.Lock()
    refresh_last: float = -math.inf

    @api.post("/refresh", **EXTERNAL)
    async def refresh(_user: str = Depends(auth)) -> dict[str, Any]:
        if scheduler is None:
            raise HTTPException(
                status_code=501,
                detail="refresh unavailable (no scheduler configured)",
            )
        nonlocal refresh_last
        now = time.monotonic()
        with refresh_lock:
            elapsed = now - refresh_last
            if elapsed < _REFRESH_MIN_INTERVAL_SECONDS:
                remaining = math.ceil(_REFRESH_MIN_INTERVAL_SECONDS - elapsed)
                raise HTTPException(
                    status_code=429,
                    detail=f"refresh rate-limited; retry in {remaining}s",
                )
            refresh_last = now
        # Run the fetch off the event loop (it does provider network I/O) and
        # wait for it so the client's follow-up /readings poll returns fresh
        # data rather than a race with a background fetch.
        await asyncio.to_thread(scheduler.fetch_now)
        return {"status": "ok", "refreshed": True}

    @api.get("/history", **EXTERNAL)
    async def get_history(
        provider: str,
        hours: float = 24.0,
        _user: str = Depends(auth),
    ) -> dict[str, Any]:
        # Stored readings for one provider over a trailing window, oldest
        # first — for switchboard/operator trend queries. Authenticated like
        # /readings (unlike /dashboard, which is deliberately open).
        try:
            prov = Provider(provider)
        except ValueError:
            valid = ", ".join(sorted(p.value for p in Provider))
            raise HTTPException(
                status_code=400,
                detail=f"unknown provider {provider!r} (valid: {valid})",
            ) from None
        if not 0 < hours <= _MAX_HISTORY_HOURS:
            raise HTTPException(
                status_code=400,
                detail=f"hours must be in (0, {_MAX_HISTORY_HOURS}]",
            )
        since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
        readings = db.get_readings_since(prov, since)
        return {
            "provider": prov.value,
            "hours": hours,
            "readings": [r.to_dict() for r in readings],
        }

    @app.get("/")
    async def root() -> RedirectResponse:
        # Bare hostname → the dashboard, so the presented URL is just the host.
        return RedirectResponse(url="/dashboard")

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard() -> HTMLResponse:
        # Unauthenticated by design: intended for private networks only, and
        # exposes nothing beyond what the display already shows.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        return HTMLResponse(_render_dashboard_html(_reported_readings(), now))

    @api.get("/schedule", **EXTERNAL)
    async def get_schedule(
        unit: str | None = None,
        _user: str = Depends(auth),
    ) -> dict[str, str | None]:
        # Raw spec for the requesting unit (?unit=<UNIT_ID>), or the default,
        # or null. The client parses/validates and falls back on its own.
        spec = schedule_config.for_unit(unit) if schedule_config is not None else None
        return {"schedule": spec}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # Mount the authenticated surface twice: canonically under /api/v1, and at
    # the legacy root paths for the fleet still on the old client. Same router
    # object, so a route can never exist on one set and not the other.
    app.include_router(api, prefix=API_V1_PREFIX)
    app.include_router(api, prefix=LEGACY_ALIAS_PREFIX)

    return app
