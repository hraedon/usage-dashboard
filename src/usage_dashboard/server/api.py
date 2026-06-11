from __future__ import annotations

import hmac
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from usage_dashboard.server.db import Database
from usage_dashboard.shared.models import (
    Provider,
    Reading,
    make_offline_reading,
)

_bearer_scheme = HTTPBearer(auto_error=False)


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

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
