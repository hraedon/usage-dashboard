from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from usage_dashboard.shared.models import Provider, Reading


class Database:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def initialize(self) -> None:
        """Create the append-only schema, migrating from the old single-row
        layout if needed.

        The old schema had ``PRIMARY KEY (provider)`` and a
        ``consecutive_failures`` column on the readings row itself; every fetch
        upserted in place, destroying history. The new schema is append-only
        (autoincrement id) with failure state in a separate provider_state
        table. This migration handles a fresh DB, an old single-row DB, and
        the intermediate states where detail/models/throttle were added via
        ALTER TABLE.
        """
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_state (
                    provider TEXT PRIMARY KEY,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._conn.commit()
            cols = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(readings)").fetchall()
            }
            if cols:
                needs_migration = "consecutive_failures" in cols or "id" not in cols
                if needs_migration:
                    self._migrate_readings(cols)
                else:
                    # Idempotent column add for an existing append-only DB that
                    # predates scoped_limits (per-model windows, e.g. Fable).
                    if "scoped_limits" not in cols:
                        self._conn.execute(
                            "ALTER TABLE readings ADD COLUMN scoped_limits TEXT"
                        )
                    self._ensure_indexes()
                self._conn.commit()
                return
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL,
                    session_percent REAL,
                    session_resets_at TEXT,
                    weekly_percent REAL,
                    weekly_resets_at TEXT,
                    fetched_at TEXT NOT NULL,
                    stale INTEGER NOT NULL DEFAULT 0,
                    detail TEXT,
                    models TEXT,
                    throttle TEXT NOT NULL DEFAULT 'none',
                    scoped_limits TEXT
                )
                """
            )
            self._ensure_indexes()
            self._conn.commit()

    def _ensure_indexes(self) -> None:
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_readings_provider_time
            ON readings (provider, fetched_at DESC)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_readings_fetched_at
            ON readings (fetched_at)
            """
        )

    def _migrate_readings(self, cols: set[str]) -> None:
        """Migrate from a pre-append-only schema to the append-only layout.

        *cols* is the set of columns on the existing ``readings`` table. The
        old single-row schema may be missing detail/models/throttle columns
        (added via ALTER TABLE in the prior code path). We rebuild a clean
        table with the canonical column set.
        """
        self._conn.execute("DROP TABLE IF EXISTS readings_new")
        self._conn.execute(
            """
            CREATE TABLE readings_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                status TEXT NOT NULL,
                session_percent REAL,
                session_resets_at TEXT,
                weekly_percent REAL,
                weekly_resets_at TEXT,
                fetched_at TEXT NOT NULL,
                stale INTEGER NOT NULL DEFAULT 0,
                detail TEXT,
                models TEXT,
                throttle TEXT NOT NULL DEFAULT 'none',
                scoped_limits TEXT
            )
            """
        )
        has_cf = "consecutive_failures" in cols
        all_cols = [
            "provider",
            "status",
            "session_percent",
            "session_resets_at",
            "weekly_percent",
            "weekly_resets_at",
            "fetched_at",
            "stale",
            "detail",
            "models",
            "throttle",
            "scoped_limits",
        ]
        present = [c for c in all_cols if c in cols]
        select_list = ", ".join(present)
        rows = self._conn.execute(
            f"SELECT {select_list} FROM readings"
        ).fetchall()
        for row in rows:
            values: list[Any] = []
            for c in all_cols:
                if c in present:
                    values.append(row[c])
                elif c == "detail":
                    values.append(None)
                elif c == "models":
                    values.append(None)
                elif c == "throttle":
                    values.append("none")
                elif c == "scoped_limits":
                    values.append(None)
            insert_list = ", ".join(all_cols)
            placeholders = ", ".join(["?"] * len(all_cols))
            self._conn.execute(
                f"INSERT INTO readings_new ({insert_list}) VALUES ({placeholders})",
                tuple(values),
            )
        if has_cf:
            cf_rows = self._conn.execute(
                "SELECT provider, consecutive_failures FROM readings"
            ).fetchall()
            for row in cf_rows:
                self._conn.execute(
                    """
                    INSERT INTO provider_state (provider, consecutive_failures)
                    VALUES (?, ?)
                    ON CONFLICT(provider) DO UPDATE SET
                        consecutive_failures = excluded.consecutive_failures
                    """,
                    (row["provider"], int(row["consecutive_failures"])),
                )
        self._conn.execute("DROP TABLE readings")
        self._conn.execute("ALTER TABLE readings_new RENAME TO readings")
        self._ensure_indexes()

    def store_reading(self, reading: Reading) -> None:
        """Append a reading row to the append-only readings table."""
        session_resets_at = (
            reading.session_resets_at.isoformat() if reading.session_resets_at is not None else None
        )
        weekly_resets_at = (
            reading.weekly_resets_at.isoformat() if reading.weekly_resets_at is not None else None
        )
        models_json = (
            json.dumps([m.to_dict() for m in reading.models])
            if reading.models
            else None
        )
        scoped_json = (
            json.dumps([s.to_dict() for s in reading.scoped_limits])
            if reading.scoped_limits
            else None
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO readings (provider, status, session_percent, session_resets_at,
                                      weekly_percent, weekly_resets_at, fetched_at, stale,
                                      detail, models, throttle, scoped_limits)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    reading.detail,
                    models_json,
                    reading.throttle,
                    scoped_json,
                ),
            )
            self._conn.commit()

    def get_latest_readings(self) -> dict[Provider, Reading]:
        with self._lock:
            cursor = self._conn.execute(
                """
                SELECT * FROM readings WHERE id IN (
                    SELECT MAX(id) FROM readings GROUP BY provider
                )
                """
            )
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
                "detail": row["detail"],
                "models": json.loads(row["models"]) if row["models"] else None,
                "throttle": row["throttle"],
                "scoped_limits": (
                    json.loads(row["scoped_limits"])
                    if "scoped_limits" in row.keys() and row["scoped_limits"]
                    else None
                ),
            }
            reading = Reading.from_dict(data)
            result[reading.provider] = reading
        return result

    def get_consecutive_failures(self, provider: Provider) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT consecutive_failures FROM provider_state WHERE provider = ?",
                (provider.value,),
            )
            row = cursor.fetchone()
        if row is None:
            return 0
        return int(row["consecutive_failures"])

    def increment_failures(self, provider: Provider) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT consecutive_failures FROM provider_state WHERE provider = ?",
                (provider.value,),
            )
            row = cursor.fetchone()
            if row is None:
                new_count = 1
                self._conn.execute(
                    "INSERT INTO provider_state (provider, consecutive_failures) VALUES (?, ?)",
                    (provider.value, new_count),
                )
            else:
                new_count = int(row["consecutive_failures"]) + 1
                self._conn.execute(
                    "UPDATE provider_state SET consecutive_failures = ? WHERE provider = ?",
                    (new_count, provider.value),
                )
            self._conn.commit()
        return new_count

    def reset_failures(self, provider: Provider) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO provider_state (provider, consecutive_failures)
                VALUES (?, 0)
                ON CONFLICT(provider) DO UPDATE SET consecutive_failures = 0
                """,
                (provider.value,),
            )
            self._conn.commit()

    def prune_old_readings(self, retention_days: int) -> int:
        """Delete readings older than *retention_days* days, always keeping the
        latest row per provider (so a long outage doesn't erase the last known
        state). Returns the count of rows removed."""
        cutoff = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(days=retention_days)
        ).isoformat()
        with self._lock:
            cursor = self._conn.execute(
                """
                DELETE FROM readings
                WHERE fetched_at < ?
                  AND id NOT IN (SELECT MAX(id) FROM readings GROUP BY provider)
                """,
                (cutoff,),
            )
            self._conn.commit()
            return cursor.rowcount
