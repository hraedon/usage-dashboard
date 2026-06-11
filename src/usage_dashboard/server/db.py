from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from usage_dashboard.shared.models import Provider, Reading


class Database:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def initialize(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS readings (
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL,
                    session_percent REAL,
                    session_resets_at TEXT,
                    weekly_percent REAL,
                    weekly_resets_at TEXT,
                    fetched_at TEXT NOT NULL,
                    stale INTEGER NOT NULL DEFAULT 0,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (provider)
                )
                """
            )
            self._conn.commit()

    def store_reading(self, reading: Reading, consecutive_failures: int = 0) -> None:
        session_resets_at = (
            reading.session_resets_at.isoformat() if reading.session_resets_at is not None else None
        )
        weekly_resets_at = (
            reading.weekly_resets_at.isoformat() if reading.weekly_resets_at is not None else None
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO readings (provider, status, session_percent, session_resets_at,
                                      weekly_percent, weekly_resets_at, fetched_at, stale,
                                      consecutive_failures)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    status = excluded.status,
                    session_percent = excluded.session_percent,
                    session_resets_at = excluded.session_resets_at,
                    weekly_percent = excluded.weekly_percent,
                    weekly_resets_at = excluded.weekly_resets_at,
                    fetched_at = excluded.fetched_at,
                    stale = excluded.stale,
                    consecutive_failures = excluded.consecutive_failures
                """,
                (
                    reading.provider.value,
                    reading.status.value,
                    reading.session_percent,
                    session_resets_at,
                    reading.weekly_percent,
                    weekly_resets_at,
                    reading.fetched_at.isoformat(),
                    int(reading.stale),
                    consecutive_failures,
                ),
            )
            self._conn.commit()

    def get_latest_readings(self) -> dict[Provider, Reading]:
        with self._lock:
            cursor = self._conn.execute("SELECT * FROM readings")
            rows = cursor.fetchall()

        result: dict[Provider, Reading] = {}
        for row in rows:
            data: dict[str, Any] = {
                "provider": row["provider"],
                "status": row["status"],
                "session_percent": row["session_percent"],
                "session_resets_at": row["session_resets_at"],
                "weekly_percent": row["weekly_percent"],
                "weekly_resets_at": row["weekly_resets_at"],
                "fetched_at": row["fetched_at"],
                "stale": bool(row["stale"]),
            }
            reading = Reading.from_dict(data)
            result[reading.provider] = reading
        return result

    def get_consecutive_failures(self, provider: Provider) -> int:
        with self._lock:
            return self._get_consecutive_failures_locked(provider)

    def _get_consecutive_failures_locked(self, provider: Provider) -> int:
        cursor = self._conn.execute(
            "SELECT consecutive_failures FROM readings WHERE provider = ?",
            (provider.value,),
        )
        row = cursor.fetchone()
        if row is None:
            return 0
        return int(row["consecutive_failures"])

    def increment_failures(self, provider: Provider) -> int:
        with self._lock:
            current = self._get_consecutive_failures_locked(provider)
            new_count = current + 1
            if current == 0:
                existing = self._conn.execute(
                    "SELECT 1 FROM readings WHERE provider = ?",
                    (provider.value,),
                ).fetchone()
                if existing is None:
                    now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                    self._conn.execute(
                        """
                        INSERT INTO readings
                            (provider, consecutive_failures, status, fetched_at, stale)
                        VALUES (?, ?, 'current', ?, 0)
                        """,
                        (provider.value, new_count, now_iso),
                    )
                else:
                    self._conn.execute(
                        "UPDATE readings SET consecutive_failures = ? WHERE provider = ?",
                        (new_count, provider.value),
                    )
            else:
                self._conn.execute(
                    "UPDATE readings SET consecutive_failures = ? WHERE provider = ?",
                    (new_count, provider.value),
                )
            self._conn.commit()
        return new_count

    def reset_failures(self, provider: Provider) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE readings SET consecutive_failures = 0 WHERE provider = ?",
                (provider.value,),
            )
            self._conn.commit()
