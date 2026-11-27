"""Durable cadence state for routine auction refreshes.

This small runtime store is deliberately separate from the archive schema:
it can be enabled without migrating or rewriting the owner's collection DB.
All timestamps are timezone-aware UTC instants; callers own the actual
collection operation and mark its outcome only after it finishes.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_REFRESH_INTERVAL = timedelta(hours=12)
MAX_RETRY_DELAY = timedelta(hours=6)


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stored timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class RoutineScheduleEntry:
    auction_id: str
    next_due_at: datetime
    last_attempted_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    last_error: str | None


class RoutineSchedule:
    """Persistent due-time and retry state for auction-level routine scans."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS routine_refresh_schedule (
                    auction_id TEXT PRIMARY KEY,
                    next_due_at TEXT NOT NULL,
                    last_attempted_at TEXT,
                    last_success_at TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0
                        CHECK (consecutive_failures >= 0),
                    last_error TEXT
                )
                """
            )

    def register(
        self,
        auction_id: str,
        *,
        due_at: datetime,
    ) -> None:
        """Register a discovered auction without postponing an earlier due time."""
        if not isinstance(auction_id, str) or not auction_id.strip():
            raise ValueError("auction_id must be a non-empty string")
        due = _timestamp(due_at)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO routine_refresh_schedule (auction_id, next_due_at)
                VALUES (?, ?)
                ON CONFLICT(auction_id) DO UPDATE SET
                    next_due_at = MIN(routine_refresh_schedule.next_due_at,
                                      excluded.next_due_at)
                """,
                (auction_id.strip(), due),
            )

    def due(self, *, now: datetime, limit: int = 20) -> tuple[RoutineScheduleEntry, ...]:
        """Return oldest due auctions first, with stable ID tie-breaking."""
        current = _timestamp(now)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT auction_id, next_due_at, last_attempted_at, last_success_at,
                       consecutive_failures, last_error
                FROM routine_refresh_schedule
                WHERE next_due_at <= ?
                ORDER BY next_due_at, auction_id
                LIMIT ?
                """,
                (current, limit),
            ).fetchall()
        return tuple(
            RoutineScheduleEntry(
                auction_id=row[0],
                next_due_at=_datetime(row[1]),
                last_attempted_at=_datetime(row[2]) if row[2] else None,
                last_success_at=_datetime(row[3]) if row[3] else None,
                consecutive_failures=row[4],
                last_error=row[5],
            )
            for row in rows
        )

    def mark_started(self, auction_id: str, *, at: datetime) -> None:
        with sqlite3.connect(self.path) as connection:
            cursor = connection.execute(
                """
                UPDATE routine_refresh_schedule
                SET last_attempted_at = ?
                WHERE auction_id = ?
                """,
                (_timestamp(at), auction_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown auction schedule: {auction_id}")

    def mark_succeeded(
        self,
        auction_id: str,
        *,
        at: datetime,
        interval: timedelta = DEFAULT_REFRESH_INTERVAL,
    ) -> None:
        if interval.total_seconds() <= 0:
            raise ValueError("interval must be positive")
        completed = _timestamp(at)
        next_due = _timestamp(at + interval)
        with sqlite3.connect(self.path) as connection:
            cursor = connection.execute(
                """
                UPDATE routine_refresh_schedule
                SET last_success_at = ?, next_due_at = ?,
                    consecutive_failures = 0, last_error = NULL
                WHERE auction_id = ?
                """,
                (completed, next_due, auction_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown auction schedule: {auction_id}")

    def mark_failed(
        self,
        auction_id: str,
        *,
        at: datetime,
        error: str,
        base_delay: timedelta = timedelta(minutes=5),
    ) -> timedelta:
        """Schedule bounded exponential retry; never mark a failed scan successful."""
        if not error.strip():
            raise ValueError("error must be non-empty")
        if base_delay.total_seconds() <= 0:
            raise ValueError("base_delay must be positive")
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT consecutive_failures FROM routine_refresh_schedule "
                "WHERE auction_id = ?",
                (auction_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown auction schedule: {auction_id}")
            failures = row[0] + 1
            delay = min(base_delay * (2 ** (failures - 1)), MAX_RETRY_DELAY)
            connection.execute(
                """
                UPDATE routine_refresh_schedule
                SET consecutive_failures = ?, next_due_at = ?, last_error = ?
                WHERE auction_id = ?
                """,
                (failures, _timestamp(at + delay), error[:1000], auction_id),
            )
        return delay

    def get(self, auction_id: str) -> RoutineScheduleEntry:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                """
                SELECT auction_id, next_due_at, last_attempted_at, last_success_at,
                       consecutive_failures, last_error
                FROM routine_refresh_schedule WHERE auction_id = ?
                """,
                (auction_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown auction schedule: {auction_id}")
        return RoutineScheduleEntry(
            auction_id=row[0],
            next_due_at=_datetime(row[1]),
            last_attempted_at=_datetime(row[2]) if row[2] else None,
            last_success_at=_datetime(row[3]) if row[3] else None,
            consecutive_failures=row[4],
            last_error=row[5],
        )
