from __future__ import annotations

import hmac
import html
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from usage_dashboard.server.db import Database
from usage_dashboard.server.schedule_config import ScheduleConfig
from usage_dashboard.shared.models import (
    Provider,
    Reading,
    ReadingStatus,
    make_offline_reading,
)

_bearer_scheme = HTTPBearer(auto_error=False)

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
        if reading.provider == Provider.UMANS:
            detail = html.escape(reading.detail) if reading.detail else "&mdash;"
            body = f'<div class="detail">{detail}</div>'
        elif reading.provider == Provider.CLAUDE and work is not None:
            body = _account_rows(reading, now, "me") + _account_rows(work, now, "work")
        else:
            body = _account_rows(reading, now)
        cards.append(f'<section class="card"><h2>{name}</h2>{body}</section>')

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
) -> FastAPI:
    app = FastAPI()
    auth = _make_auth_dependency(api_key)

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

    @app.get("/readings")
    async def get_readings(
        _user: str = Depends(auth),
    ) -> list[dict[str, Any]]:
        return [reading.to_dict() for reading in _reported_readings()]

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

    @app.get("/schedule")
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

    return app
