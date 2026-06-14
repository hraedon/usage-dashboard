from __future__ import annotations

import hmac
import html
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from usage_dashboard.server.db import Database
from usage_dashboard.shared.models import (
    Provider,
    Reading,
    ReadingStatus,
    make_offline_reading,
)

_bearer_scheme = HTTPBearer(auto_error=False)

# Same thresholds as the Pi client's _bar_color (client/renderer.py)
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


def _render_dashboard_html(readings: list[Reading], now: datetime) -> str:
    cards: list[str] = []
    for reading in readings:
        name = html.escape(reading.provider.value.upper()) + _status_badge(reading)
        if reading.provider == Provider.UMANS:
            detail = html.escape(reading.detail) if reading.detail else "&mdash;"
            body = f'<div class="detail">{detail}</div>'
        else:
            body = _bar_row(
                "Session", reading.session_percent, reading.session_resets_at, now
            ) + _bar_row("Weekly", reading.weekly_percent, reading.weekly_resets_at, now)
        cards.append(f'<section class="card"><h2>{name}</h2>{body}</section>')

    fetched = max((r.fetched_at for r in readings), default=now)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>AI Usage</title>
<style>
body {{ background:#000; color:#fff; font-family:-apple-system,system-ui,sans-serif;
  margin:0; padding:12px; }}
.card {{ background:#111; border-radius:12px; padding:14px 16px; margin-bottom:12px; }}
h2 {{ margin:0 0 10px; font-size:1.05rem; letter-spacing:0.04em; }}
.badge {{ font-size:0.7rem; color:#eab308; border:1px solid #eab308;
  border-radius:6px; padding:1px 6px; vertical-align:middle; }}
.row {{ display:flex; align-items:center; gap:10px; }}
.label {{ width:60px; font-size:0.85rem; color:#ccc; }}
.track {{ flex:1; height:12px; background:#323232; border-radius:6px; overflow:hidden;
  display:block; }}
.fill {{ display:block; height:100%; }}
.pct {{ width:44px; text-align:right; font-variant-numeric:tabular-nums; }}
.resets {{ margin:2px 0 8px 70px; font-size:0.75rem; color:#969696; }}
.detail {{ font-size:1.0rem; color:#ccc; font-variant-numeric:tabular-nums; }}
footer {{ text-align:center; color:#555; font-size:0.7rem; margin-top:8px; }}
</style>
</head>
<body>
{"".join(cards)}
<footer>fetched {fetched.strftime("%Y-%m-%d %H:%M:%S")} UTC &middot; refreshes every 5&ndash;30m (adaptive)</footer>
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


def create_app(api_key: str, db: Database) -> FastAPI:
    app = FastAPI()
    auth = _make_auth_dependency(api_key)

    @app.get("/readings")
    async def get_readings(
        _user: str = Depends(auth),
    ) -> list[dict[str, Any]]:
        readings: dict[Provider, Reading] = db.get_latest_readings()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        result: list[dict[str, Any]] = []
        for provider in Provider:
            reading = readings.get(provider)
            if reading is None:
                reading = make_offline_reading(provider, now)
            result.append(reading.to_dict())
        return result

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard() -> HTMLResponse:
        # Unauthenticated by design: intended for private networks only, and
        # exposes nothing beyond what the display already shows.
        readings: dict[Provider, Reading] = db.get_latest_readings()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        ordered = [readings.get(p) or make_offline_reading(p, now) for p in Provider]
        return HTMLResponse(_render_dashboard_html(ordered, now))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
