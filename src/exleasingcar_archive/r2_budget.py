"""Conservative application-side guardrails for Cloudflare R2 usage."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

FREE_TIER_CLASS_A_REQUESTS = 1_000_000
FREE_TIER_CLASS_B_REQUESTS = 10_000_000
FREE_TIER_STORAGE_BYTES = 10_000_000_000

# Start at 50% of the currently documented Standard free allocation.
DEFAULT_MAX_CLASS_A_REQUESTS = 500_000
DEFAULT_MAX_CLASS_B_REQUESTS = 5_000_000
DEFAULT_MAX_STORAGE_BYTES = 5_000_000_000
ROLLING_WINDOW_DAYS = 31


class R2BudgetExceeded(RuntimeError):
    """Raised before an R2 request would exceed the local safety budget."""


@dataclass(frozen=True, slots=True)
class R2UsageSnapshot:
    """Account-wide usage returned by Cloudflare's R2 analytics datasets."""

    class_a_requests: int
    class_b_requests: int
    storage_peak_bytes: int

    def __post_init__(self) -> None:
        for name, value in (
            ("class_a_requests", self.class_a_requests),
            ("class_b_requests", self.class_b_requests),
            ("storage_peak_bytes", self.storage_peak_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class R2BudgetLimits:
    """Hard application ceilings. Runtime configuration can only lower them."""

    class_a_requests: int = DEFAULT_MAX_CLASS_A_REQUESTS
    class_b_requests: int = DEFAULT_MAX_CLASS_B_REQUESTS
    storage_bytes: int = DEFAULT_MAX_STORAGE_BYTES

    def __post_init__(self) -> None:
        limits = (
            ("class_a_requests", self.class_a_requests, DEFAULT_MAX_CLASS_A_REQUESTS),
            ("class_b_requests", self.class_b_requests, DEFAULT_MAX_CLASS_B_REQUESTS),
            ("storage_bytes", self.storage_bytes, DEFAULT_MAX_STORAGE_BYTES),
        )
        for name, value, ceiling in limits:
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if not 1 <= value <= ceiling:
                raise ValueError(
                    f"{name} must be between 1 and the safe ceiling {ceiling}"
                )


class R2UsageBudget:
    """A durable, multi-process ledger for all R2 calls made by this application.

    The rolling 31-day operation counters are deliberately stricter than a
    calendar-month allowance. Object reservations are never automatically
    released: an ambiguous upload failure must not create extra headroom.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        snapshot: R2UsageSnapshot,
        limits: R2BudgetLimits | None = None,
    ) -> None:
        self.database_path = str(database_path)
        self.snapshot = snapshot
        self.limits = limits or R2BudgetLimits()
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @classmethod
    def from_env(
        cls,
        snapshot: R2UsageSnapshot,
        environ: dict[str, str] | None = None,
    ) -> R2UsageBudget:
        values = os.environ if environ is None else environ

        def read_limit(name: str, default: int) -> int:
            raw = values.get(name)
            if raw is None or not raw.strip():
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise ValueError(f"{name} must be an integer") from exc

        limits = R2BudgetLimits(
            class_a_requests=read_limit(
                "EXLEASINGCAR_R2_BUDGET_MAX_CLASS_A",
                DEFAULT_MAX_CLASS_A_REQUESTS,
            ),
            class_b_requests=read_limit(
                "EXLEASINGCAR_R2_BUDGET_MAX_CLASS_B",
                DEFAULT_MAX_CLASS_B_REQUESTS,
            ),
            storage_bytes=read_limit(
                "EXLEASINGCAR_R2_BUDGET_MAX_STORAGE_BYTES",
                DEFAULT_MAX_STORAGE_BYTES,
            ),
        )
        path = values.get(
            "EXLEASINGCAR_R2_BUDGET_DATABASE",
            "data/r2_usage_budget.sqlite3",
        )
        return cls(path, snapshot=snapshot, limits=limits)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS r2_usage_hourly (
                    hour_utc TEXT PRIMARY KEY,
                    class_a_count INTEGER NOT NULL DEFAULT 0,
                    class_b_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS r2_object_reservations (
                    object_key TEXT PRIMARY KEY,
                    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
                    reserved_at TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _hour_key(now: datetime) -> str:
        return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")

    @staticmethod
    def _window_start(now: datetime) -> str:
        start = now.astimezone(timezone.utc) - timedelta(days=ROLLING_WINDOW_DAYS)
        return start.strftime("%Y-%m-%dT%H:00:00Z")

    @staticmethod
    def _counts(connection: sqlite3.Connection, start: str) -> tuple[int, int]:
        row = connection.execute(
            """
            SELECT COALESCE(SUM(class_a_count), 0) AS class_a,
                   COALESCE(SUM(class_b_count), 0) AS class_b
            FROM r2_usage_hourly
            WHERE hour_utc >= ?
            """,
            (start,),
        ).fetchone()
        return int(row["class_a"]), int(row["class_b"])

    def reserve_operation(self, operation_class: Literal["class_a", "class_b", "unknown"]) -> None:
        """Reserve one request before sending it to R2.

        Unknown analytics/API operations consume both safety counters, rather
        than being silently ignored.
        """
        now = self._now()
        hour = self._hour_key(now)
        start = self._window_start(now)
        increment_a = int(operation_class in {"class_a", "unknown"})
        increment_b = int(operation_class in {"class_b", "unknown"})

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO r2_usage_hourly (hour_utc, class_a_count, class_b_count)
                VALUES (?, 0, 0)
                ON CONFLICT(hour_utc) DO NOTHING
                """,
                (hour,),
            )
            connection.execute(
                """
                UPDATE r2_usage_hourly
                SET class_a_count = class_a_count + ?,
                    class_b_count = class_b_count + ?
                WHERE hour_utc = ?
                """,
                (increment_a, increment_b, hour),
            )
            local_a, local_b = self._counts(connection, start)
            local_storage_row = connection.execute(
                "SELECT COALESCE(SUM(byte_size), 0) AS total FROM r2_object_reservations"
            ).fetchone()
            effective_storage = max(
                int(local_storage_row["total"]),
                self.snapshot.storage_peak_bytes,
            )
            effective_a = max(local_a, self.snapshot.class_a_requests)
            effective_b = max(local_b, self.snapshot.class_b_requests)
            if effective_storage >= self.limits.storage_bytes:
                connection.rollback()
                raise R2BudgetExceeded(
                    "R2 storage safety budget reached; no R2 request was sent"
                )
            if effective_a > self.limits.class_a_requests:
                connection.rollback()
                raise R2BudgetExceeded(
                    "R2 Class A safety budget reached; no R2 request was sent"
                )
            if effective_b > self.limits.class_b_requests:
                connection.rollback()
                raise R2BudgetExceeded(
                    "R2 Class B safety budget reached; no R2 request was sent"
                )
            connection.commit()

    def reserve_put(self, object_key: str, byte_size: int) -> None:
        """Atomically reserve one Class A request and the object's storage."""
        if not isinstance(object_key, str) or not object_key.strip():
            raise ValueError("object_key cannot be empty")
        if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 0:
            raise ValueError("byte_size must be a non-negative integer")

        now = self._now()
        hour = self._hour_key(now)
        start = self._window_start(now)
        reserved_at = now.isoformat()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT byte_size FROM r2_object_reservations WHERE object_key = ?
                """,
                (object_key,),
            ).fetchone()
            if existing is not None and int(existing["byte_size"]) != byte_size:
                connection.rollback()
                raise R2BudgetExceeded(
                    "An existing object key has a different reserved size"
                )

            connection.execute(
                """
                INSERT INTO r2_usage_hourly (hour_utc, class_a_count, class_b_count)
                VALUES (?, 0, 0)
                ON CONFLICT(hour_utc) DO NOTHING
                """,
                (hour,),
            )
            connection.execute(
                """
                UPDATE r2_usage_hourly
                SET class_a_count = class_a_count + 1
                WHERE hour_utc = ?
                """,
                (hour,),
            )
            local_a, local_b = self._counts(connection, start)
            if max(local_a, self.snapshot.class_a_requests) > self.limits.class_a_requests:
                connection.rollback()
                raise R2BudgetExceeded(
                    "R2 Class A safety budget reached; upload was not sent"
                )
            if max(local_b, self.snapshot.class_b_requests) > self.limits.class_b_requests:
                connection.rollback()
                raise R2BudgetExceeded(
                    "R2 Class B safety budget reached; upload was not sent"
                )

            row = connection.execute(
                """
                SELECT COALESCE(SUM(byte_size), 0) AS reserved_bytes
                FROM r2_object_reservations
                """
            ).fetchone()
            local_storage = int(row["reserved_bytes"])
            effective_storage = max(
                local_storage,
                self.snapshot.storage_peak_bytes,
            )
            proposed_storage = effective_storage + (
                byte_size if existing is None else 0
            )
            if proposed_storage > self.limits.storage_bytes:
                connection.rollback()
                raise R2BudgetExceeded(
                    "R2 storage safety budget reached; upload was not sent"
                )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO r2_object_reservations (
                        object_key, byte_size, reserved_at
                    ) VALUES (?, ?, ?)
                    """,
                    (object_key, byte_size, reserved_at),
                )
            connection.commit()

    def ensure_available(self) -> None:
        """Fail before a batch starts when any safety threshold is exhausted."""
        report = self.report()
        exhausted = (
            report["class_a_used"] >= self.limits.class_a_requests
            or report["class_b_used"] >= self.limits.class_b_requests
            or report["storage_used_bytes"] >= self.limits.storage_bytes
        )
        if exhausted:
            raise R2BudgetExceeded(
                "An R2 safety threshold is already exhausted; no batch was started"
            )
    def report(self) -> dict[str, int]:
        """Return conservative effective usage without exposing credentials."""
        now = self._now()
        start = self._window_start(now)
        with self._connect() as connection:
            local_a, local_b = self._counts(connection, start)
            row = connection.execute(
                """
                SELECT COALESCE(SUM(byte_size), 0) AS reserved_bytes
                FROM r2_object_reservations
                """
            ).fetchone()
        effective_a = max(local_a, self.snapshot.class_a_requests)
        effective_b = max(local_b, self.snapshot.class_b_requests)
        effective_storage = max(
            int(row["reserved_bytes"]),
            self.snapshot.storage_peak_bytes,
        )
        return {
            "class_a_used": effective_a,
            "class_a_limit": self.limits.class_a_requests,
            "class_a_remaining": max(0, self.limits.class_a_requests - effective_a),
            "class_b_used": effective_b,
            "class_b_limit": self.limits.class_b_requests,
            "class_b_remaining": max(0, self.limits.class_b_requests - effective_b),
            "storage_used_bytes": effective_storage,
            "storage_limit_bytes": self.limits.storage_bytes,
            "storage_remaining_bytes": max(
                0, self.limits.storage_bytes - effective_storage
            ),
        }
