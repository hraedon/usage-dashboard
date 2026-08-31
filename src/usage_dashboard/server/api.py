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
from usage_dashboard.shared.offpeak import (
    zai_is_offpeak,
    zai_peak_label,
)

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
# Spread these into the route decorator. `openapi_extra` surfaces the marker in
# the OpenAPI schema, which is where the contract test reads it from — the only
# representation stable across fastapi versions (0.141 stopped exposing
# flattened routes on `app.routes`). The test requires EVERY authenticated route
# to carry a marker: an undeclared route fails the build rather than defaulting
# either way, so adding a route forces the decision.
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
_CSS_YELLOW = "#eab308"

# The web view deliberately has its own order.  ``Provider`` order follows
# the scheduler's wiring (and includes the folded Claude work account), while
# this is the order a person scans the dashboard in.  OpenCode Go is a web-only
# card; the touch layout intentionally does not include it.
_WEB_PROVIDER_ORDER: tuple[Provider, ...] = (
    Provider.CLAUDE,
    Provider.CODEX,
    Provider.ZAI,
    Provider.OLLAMA,
    Provider.OPENCODE,
)
_WEB_PROVIDER_NAMES: dict[Provider, str] = {
    Provider.CLAUDE: "Claude",
    Provider.CODEX: "Codex",
    Provider.ZAI: "ZAI",
    Provider.OLLAMA: "Ollama",
    Provider.OPENCODE: "OpenCode Go",
}

# Keep these values together with the generated CSS so the responsive contract
# is easy to test without a browser.  At 1280px, body padding leaves 1256px:
# four 260px cards plus three 12px gaps fit.  At 320–390px only one does.
_WEB_BODY_PADDING = 12
_WEB_MAX_WIDTH = 1280
_WEB_GRID_MIN_WIDTH = 260
_WEB_GRID_GAP = 12
_RESET_NEAR_SECONDS = 3 * 24 * 60 * 60


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


def _status_name(reading: Reading) -> str:
    """Return the visual status class for *reading*.

    Offline is intentionally checked before stale: an offline reading carries
    ``stale=True`` as part of the model, but the stronger state must remain
    visible to a glance.
    """
    if reading.status == ReadingStatus.OFFLINE:
        return "offline"
    if reading.status == ReadingStatus.STALE or reading.stale:
        return "stale"
    return "current"


def _worst_status(*readings: Reading | None) -> str:
    """Status for a card that may contain more than one account."""
    statuses = {_status_name(reading) for reading in readings if reading is not None}
    if "offline" in statuses:
        return "offline"
    if "stale" in statuses:
        return "stale"
    return "current"


def _status_badge_for(status: str) -> str:
    if status == "offline":
        return ' <span class="badge badge-offline">offline</span>'
    if status == "stale":
        return ' <span class="badge badge-stale">stale</span>'
    return ""


def _seconds_until_reset(resets_at: datetime, now: datetime) -> int:
    """Return whole UTC seconds until a reset, accepting naive or aware times."""
    target = (
        resets_at.replace(tzinfo=timezone.utc)
        if resets_at.tzinfo is None
        else resets_at.astimezone(timezone.utc)
    )
    current = (
        now.replace(tzinfo=timezone.utc)
        if now.tzinfo is None
        else now.astimezone(timezone.utc)
    )
    return int((target - current).total_seconds())


def _countdown_text(resets_at: datetime | None, now: datetime) -> str:
    if resets_at is None:
        return ""
    # Readings are stored as naive UTC, while direct renderer/parity tests often
    # use aware UTC datetimes.  Normalize both sides so the web renderer never
    # raises on an otherwise valid reading.
    total_seconds = _seconds_until_reset(resets_at, now)
    if total_seconds <= 0:
        return "resets 0m"
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    if days > 0:
        return f"resets {days}d {hours}h"
    return f"resets {hours}h {minutes}m"


def _countdown_state(resets_at: datetime | None, now: datetime) -> str:
    """Classify a reset the same way the touch client highlights it.

    ``near`` and ``expired`` are both urgent (the touch display uses its
    highlighted reset colour for both); only a reset more than three days away
    is ``distant``.  A missing reset has no visible countdown and is ``none``.
    """
    if resets_at is None:
        return "none"
    total_seconds = _seconds_until_reset(resets_at, now)
    if total_seconds <= 0:
        return "expired"
    if total_seconds <= _RESET_NEAR_SECONDS:
        return "near"
    return "distant"


def _status_badge(reading: Reading) -> str:
    return _status_badge_for(_status_name(reading))


def _bar_row(label: str, percent: float | None, resets_at: datetime | None, now: datetime) -> str:
    color = _bar_color_css(percent)
    width = max(0.0, min(percent, 100.0)) if percent is not None else 0.0
    pct_text = f"{percent:.0f}%" if percent is not None else "N/A"
    countdown = _countdown_text(resets_at, now)
    reset_state = _countdown_state(resets_at, now)
    reset_class = f" reset-{reset_state}" if reset_state != "none" else ""
    return (
        f'<div class="row"><span class="label">{html.escape(label)}</span>'
        f'<span class="track"><span class="fill" style="width:{width:.0f}%;'
        f'background:{color}"></span></span>'
        f'<span class="pct">{pct_text}</span></div>'
        f'<div class="resets{reset_class}" data-reset-state="{reset_state}">'
        f"{countdown}</div>"
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
    cards: list[str] = []
    for provider in _WEB_PROVIDER_ORDER:
        # The work Claude account folds into the Claude card, not its own.  If
        # only the work account exists, it still gets one correctly named
        # Claude card rather than disappearing with the folded account.
        if provider == Provider.CLAUDE:
            reading = by_provider.get(Provider.CLAUDE) or by_provider.get(
                Provider.CLAUDE_WORK
            )
        else:
            reading = by_provider.get(provider)
        if reading is None:
            continue

        work = by_provider.get(Provider.CLAUDE_WORK)
        card_status = _worst_status(
            reading,
            work if provider == Provider.CLAUDE and Provider.CLAUDE in by_provider else None,
        )
        name = html.escape(_WEB_PROVIDER_NAMES[provider]) + _status_badge_for(card_status)
        # z.ai's plan discounts off-peak use (peak = Mon–Fri 14:00–18:00
        # Singapore time): tint the card header green off-peak / orange peak so
        # "use it now vs wait it out" is glanceable from across the room.
        header_style = ""
        peak_note = ""
        if provider == Provider.ZAI:
            offpeak = zai_is_offpeak(now)
            color = _CSS_GREEN if offpeak else _CSS_ORANGE
            header_style = f' style="color:{color}"'
            # ...and say how long it lasts. The tint alone answers "is it
            # peak?" but not "for how much longer?", which is the actionable
            # half — you cannot tell from a colour whether to start a job now
            # or wait twenty minutes. Same label the panel shows (WI-030).
            peak_note = (
                f'<span class="peak" style="color:{color}">'
                f"{html.escape(zai_peak_label(now))}</span>"
            )
        if provider == Provider.CLAUDE and Provider.CLAUDE in by_provider and work is not None:
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
        cards.append(
            f'<section class="card status-{card_status}" '
            f'data-provider="{provider.value}" data-status="{card_status}">'
            f'<h2{header_style}>{name}{peak_note}</h2>'
            f"{body}</section>"
        )

    fetched = max((r.fetched_at for r in readings), default=now)
    provider_count = len(cards)
    provider_word = "provider" if provider_count == 1 else "providers"
    header = (
        '<header><h1>AI Usage <span class="provider-count">'
        f"{provider_count} {provider_word}</span></h1></header>"
    )
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
:root {{ --maxw: {_WEB_MAX_WIDTH}px; }}
* {{ box-sizing: border-box; }}
body {{ background:#000; color:#fff; font-family:-apple-system,system-ui,sans-serif;
  margin:0; padding:{_WEB_BODY_PADDING}px; min-width:0; }}
header, footer, .grid {{ width:100%; max-width:var(--maxw); margin-inline:auto; min-width:0; }}
header h1 {{ margin:4px 4px 12px; font-size:1.1rem; font-weight:600;
  letter-spacing:0.06em; color:#ddd; }}
/* Count displayed cards, not raw readings: Claude's optional work account is
   folded into the single Claude card below. */
.provider-count {{ float:right; color:#969696; font-size:0.78rem; font-weight:400;
  letter-spacing:0; }}
/* Fluid grid: 1 column on a phone, 2 on a tablet, up to 4 on a desktop,
   driven by the card min width — no per-device breakpoints needed. */
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,
  minmax(min(100%, {_WEB_GRID_MIN_WIDTH}px), 1fr));
  gap:{_WEB_GRID_GAP}px; align-items:start; }}
.card {{ background:#111; border:1px solid #292929; border-left:4px solid #3b3b3b;
  border-radius:12px; padding:14px 16px; min-width:0; }}
.card.status-stale {{ background:#17130b; border-color:#a16207;
  border-left-color:{_CSS_ORANGE}; }}
.card.status-offline {{ background:#180d0d; border-color:#991b1b;
  border-left-color:{_CSS_RED}; }}
h2 {{ margin:0 0 10px; font-size:1.05rem; letter-spacing:0.04em; }}
.badge {{ font-size:0.7rem; font-weight:700; border:1px solid;
  border-radius:6px; padding:1px 6px; vertical-align:middle; }}
.badge-stale {{ color:{_CSS_ORANGE}; border-color:{_CSS_ORANGE};
  background:#2b1c0a; }}
.badge-offline {{ color:{_CSS_RED}; border-color:{_CSS_RED};
  background:#301010; }}
/* Peak-window countdown beside a card title. Floated right so it reads as a
   subtitle rather than part of the provider name, matching the Pi, where it
   sits in the tile's subtitle slot. */
.peak {{ float:right; font-size:0.78rem; font-weight:400;
  letter-spacing:0; line-height:1.6; }}
.row {{ display:flex; align-items:center; gap:10px; min-width:0; }}
.label {{ flex:0 1 96px; min-width:0; overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap; font-size:0.85rem; color:#ccc; }}
.track {{ flex:1; height:12px; background:#323232; border-radius:6px; overflow:hidden;
  display:block; min-width:0; }}
.fill {{ display:block; height:100%; }}
.pct {{ flex:0 0 44px; width:44px; text-align:right;
  font-variant-numeric:tabular-nums; }}
.resets {{ margin:2px 0 8px 106px; font-size:0.75rem; color:#969696;
  font-variant-numeric:tabular-nums; }}
.resets.reset-near, .resets.reset-expired {{ color:{_CSS_YELLOW};
  font-weight:700; }}
.resets.reset-distant {{ color:#969696; }}
.detail {{ font-size:1.0rem; color:#ccc; font-variant-numeric:tabular-nums;
  overflow-wrap:anywhere; }}
footer {{ text-align:center; color:#555; font-size:0.7rem; margin-top:12px; }}
</style>
</head>
<body>
{header}
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
