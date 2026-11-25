"""Pure, bounded work planning and per-run accounting for the archive collector.

This module has no database, browser, or host-scheduler dependency. The
planner is deterministic, and hard run limits use an injectable monotonic
clock so runtime integration can enforce budgets without coupling tests to
wall-clock time.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class WorkType(str, Enum):
    """Collector work classes, ordered by operational priority."""

    LIVE_REFRESH = "live_refresh"
    BOOTSTRAP = "bootstrap"
    SCHEDULED_END_REFRESH = "scheduled_end_refresh"
    ROUTINE_REFRESH = "routine_refresh"
    DETAIL_ENRICHMENT = "detail_enrichment"

    @property
    def priority(self) -> int:
        return {
            WorkType.LIVE_REFRESH: 0,
            WorkType.BOOTSTRAP: 1,
            WorkType.SCHEDULED_END_REFRESH: 2,
            WorkType.ROUTINE_REFRESH: 3,
            WorkType.DETAIL_ENRICHMENT: 4,
        }[self]


class DeferredReason(str, Enum):
    """Why a work item was not included in the current batch."""

    NOT_DUE = "not_due"
    MAX_ITEMS = "max_items"
    MAX_ESTIMATED_SECONDS = "max_estimated_seconds"
    MAX_CATALOG_PAGES = "max_catalog_pages"
    MAX_LIVE_LOTS = "max_live_lots"
    MAX_DETAIL_PAGES = "max_detail_pages"


@dataclass(frozen=True, slots=True)
class CollectionWorkItem:
    """One resumable unit of collector work.

    Costs are explicit so the planner can enforce separate catalogue-page,
    live-lot, and detail-page limits. Bootstrap, scheduled-end, and routine work
    consume catalogue pages; live refresh consumes lot slots; detail
    enrichment consumes detail-page slots.
    """

    work_id: str
    work_type: WorkType
    auction_id: str
    due_at: datetime
    target_id: str | None = None
    catalog_pages: int = 0
    live_lots: int = 0
    detail_pages: int = 0
    estimated_seconds: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.work_id, str) or not self.work_id.strip():
            raise ValueError("work_id must be a non-empty string")
        if not isinstance(self.work_type, WorkType):
            raise TypeError("work_type must be a WorkType")
        if not isinstance(self.auction_id, str) or not self.auction_id.strip():
            raise ValueError("auction_id must be a non-empty string")
        if self.target_id is not None and (
            not isinstance(self.target_id, str) or not self.target_id.strip()
        ):
            raise ValueError("target_id must be a non-empty string when supplied")
        if not isinstance(self.due_at, datetime) or self.due_at.tzinfo is None:
            raise ValueError("due_at must be a timezone-aware datetime")
        if self.due_at.utcoffset() is None:
            raise ValueError("due_at must be a timezone-aware datetime")

        for field_name in ("catalog_pages", "live_lots", "detail_pages"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if (
            isinstance(self.estimated_seconds, bool)
            or not isinstance(self.estimated_seconds, int)
            or self.estimated_seconds <= 0
        ):
            raise ValueError("estimated_seconds must be a positive integer")

        costs = {
            "catalogue": self.catalog_pages,
            "live lots": self.live_lots,
            "detail pages": self.detail_pages,
        }
        expected_cost = {
            WorkType.LIVE_REFRESH: "live lots",
            WorkType.DETAIL_ENRICHMENT: "detail pages",
            WorkType.BOOTSTRAP: "catalogue",
            WorkType.PRESTART_REFRESH: "catalogue",
            WorkType.ROUTINE_REFRESH: "catalogue",
        }[self.work_type]
        if costs[expected_cost] <= 0:
            raise ValueError(f"{self.work_type.value} must consume {expected_cost}")
        if sum(value for name, value in costs.items() if name != expected_cost) != 0:
            raise ValueError(
                f"{self.work_type.value} cannot consume unrelated work budgets"
            )


@dataclass(frozen=True, slots=True)
class PlannerBudget:
    """Configurable limits for one planner invocation.

    max_estimated_seconds is a planning guard, not a substitute for the
    runtime's hard monotonic wall-clock deadline.
    """

    max_items: int
    max_estimated_seconds: int
    max_catalog_pages: int
    max_live_lots: int
    max_detail_pages: int
    reserved_background_items: int = 1

    def __post_init__(self) -> None:
        positive_fields = ("max_items", "max_estimated_seconds")
        non_negative_fields = (
            "max_catalog_pages",
            "max_live_lots",
            "max_detail_pages",
            "reserved_background_items",
        )
        for field_name in positive_fields:
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        for field_name in non_negative_fields:
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.reserved_background_items > self.max_items:
            raise ValueError("reserved_background_items cannot exceed max_items")


@dataclass(frozen=True, slots=True)
class DeferredWork:
    """A work item and the budget or due-time reason it was deferred."""

    item: CollectionWorkItem
    reason: DeferredReason


@dataclass(frozen=True, slots=True)
class PlannerResult:
    """Deterministic planner output for one invocation."""

    selected: tuple[CollectionWorkItem, ...]
    deferred: tuple[DeferredWork, ...]


class RunBudgetReason(str, Enum):
    """Why a unit of work was rejected by the hard per-run budget."""

    WALL_CLOCK = "wall_clock"
    MAX_CATALOG_PAGES = "max_catalog_pages"
    MAX_LIVE_LOTS = "max_live_lots"
    MAX_DETAIL_PAGES = "max_detail_pages"


@dataclass(frozen=True, slots=True)
class RunLimits:
    """Hard limits enforced while one collector invocation is running.

    A zero resource limit disables that work type for the invocation.
    max_duration_seconds is always positive and is measured using a monotonic
    clock, not wall-clock timestamps.
    """

    max_duration_seconds: float
    max_catalog_pages: int
    max_live_lots: int
    max_detail_pages: int

    def __post_init__(self) -> None:
        duration = self.max_duration_seconds
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise TypeError("max_duration_seconds must be a finite positive number")
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("max_duration_seconds must be a finite positive number")
        object.__setattr__(self, "max_duration_seconds", float(duration))

        for field_name in (
            "max_catalog_pages",
            "max_live_lots",
            "max_detail_pages",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class RunBudgetSnapshot:
    """Immutable accounting snapshot for one collector invocation."""

    elapsed_seconds: float
    remaining_seconds: float
    expired: bool
    catalog_pages_used: int
    live_lots_used: int
    detail_pages_used: int


class RunBudget:
    """Atomic per-run limits with an injectable monotonic clock.

    Call try_consume() before starting a unit of work. Accepted units count
    against the run even if the subsequent network/page operation fails; this
    prevents retries from evading the configured request/page budget.
    """

    def __init__(
        self,
        limits: RunLimits,
        *,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(limits, RunLimits):
            raise TypeError("limits must be a RunLimits instance")
        if not callable(monotonic_clock):
            raise TypeError("monotonic_clock must be callable")
        self.limits = limits
        self._clock = monotonic_clock
        self._started_at = self._read_clock()
        self._last_clock = self._started_at
        self._deadline = self._started_at + limits.max_duration_seconds
        self._catalog_pages_used = 0
        self._live_lots_used = 0
        self._detail_pages_used = 0

    def _read_clock(self) -> float:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("monotonic_clock must return a finite number")
        if not math.isfinite(value):
            raise ValueError("monotonic_clock must return a finite number")
        current = float(value)
        previous = getattr(self, "_last_clock", current)
        if current < previous:
            raise ValueError("monotonic_clock moved backwards")
        self._last_clock = current
        return current

    @staticmethod
    def _validate_units(value: int, field_name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{field_name} must be a non-negative integer")
        if value < 0:
            raise ValueError(f"{field_name} must be a non-negative integer")
        return value

    def try_consume(
        self,
        *,
        catalog_pages: int = 0,
        live_lots: int = 0,
        detail_pages: int = 0,
    ) -> RunBudgetReason | None:
        """Reserve units atomically, or return the first limit that blocks them.

        The clock and all resource counters are checked before any counter is
        changed. A rejected multi-resource request therefore consumes nothing.
        """
        catalog_pages = self._validate_units(catalog_pages, "catalog_pages")
        live_lots = self._validate_units(live_lots, "live_lots")
        detail_pages = self._validate_units(detail_pages, "detail_pages")
        if catalog_pages + live_lots + detail_pages == 0:
            raise ValueError("at least one work unit must be requested")

        current = self._read_clock()
        if current >= self._deadline:
            return RunBudgetReason.WALL_CLOCK
        if self._catalog_pages_used + catalog_pages > self.limits.max_catalog_pages:
            return RunBudgetReason.MAX_CATALOG_PAGES
        if self._live_lots_used + live_lots > self.limits.max_live_lots:
            return RunBudgetReason.MAX_LIVE_LOTS
        if self._detail_pages_used + detail_pages > self.limits.max_detail_pages:
            return RunBudgetReason.MAX_DETAIL_PAGES

        self._catalog_pages_used += catalog_pages
        self._live_lots_used += live_lots
        self._detail_pages_used += detail_pages
        return None

    def snapshot(self) -> RunBudgetSnapshot:
        """Return current usage and monotonic deadline information."""
        current = self._read_clock()
        elapsed = current - self._started_at
        remaining = max(0.0, self._deadline - current)
        return RunBudgetSnapshot(
            elapsed_seconds=elapsed,
            remaining_seconds=remaining,
            expired=current >= self._deadline,
            catalog_pages_used=self._catalog_pages_used,
            live_lots_used=self._live_lots_used,
            detail_pages_used=self._detail_pages_used,
        )


@dataclass(slots=True)
class _BudgetUsage:
    items: int = 0
    estimated_seconds: int = 0
    catalog_pages: int = 0
    live_lots: int = 0
    detail_pages: int = 0

    def add(self, item: CollectionWorkItem) -> None:
        self.items += 1
        self.estimated_seconds += item.estimated_seconds
        self.catalog_pages += item.catalog_pages
        self.live_lots += item.live_lots
        self.detail_pages += item.detail_pages


def _sort_key(item: CollectionWorkItem) -> tuple[int, datetime, str, str, str]:
    """Priority first, then oldest due time, with stable identifier tie-breaks."""
    return (
        item.work_type.priority,
        item.due_at,
        item.auction_id,
        item.target_id or "",
        item.work_id,
    )


def _budget_block(
    item: CollectionWorkItem,
    usage: _BudgetUsage,
    budget: PlannerBudget,
) -> DeferredReason | None:
    if usage.items >= budget.max_items:
        return DeferredReason.MAX_ITEMS
    if usage.estimated_seconds + item.estimated_seconds > budget.max_estimated_seconds:
        return DeferredReason.MAX_ESTIMATED_SECONDS
    if usage.catalog_pages + item.catalog_pages > budget.max_catalog_pages:
        return DeferredReason.MAX_CATALOG_PAGES
    if usage.live_lots + item.live_lots > budget.max_live_lots:
        return DeferredReason.MAX_LIVE_LOTS
    if usage.detail_pages + item.detail_pages > budget.max_detail_pages:
        return DeferredReason.MAX_DETAIL_PAGES
    return None


def plan_collection_work(
    items: Iterable[CollectionWorkItem],
    *,
    now: datetime,
    budget: PlannerBudget,
) -> PlannerResult:
    """Select a bounded, deterministic batch from due work candidates.

    Live work is considered first. A configurable number of non-live items is
    selected before filling the remaining slots, preventing continuous live
    activity from starving bootstrap, routine refresh, or detail enrichment.
    Future-due work is returned separately as NOT_DUE and consumes no budget.

    Persistence, retries, source validation, idempotency, and the actual
    wall-clock stop condition belong to the runtime adapter.
    """
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    if not isinstance(budget, PlannerBudget):
        raise TypeError("budget must be a PlannerBudget")

    candidates = tuple(items)
    if any(not isinstance(item, CollectionWorkItem) for item in candidates):
        raise TypeError("items must contain only CollectionWorkItem values")
    work_ids = [item.work_id for item in candidates]
    if len(work_ids) != len(set(work_ids)):
        raise ValueError("work_id values must be unique within one planning batch")

    ordered = sorted(candidates, key=_sort_key)
    due_items = [item for item in ordered if item.due_at <= now]
    deferred: list[DeferredWork] = [
        DeferredWork(item, DeferredReason.NOT_DUE)
        for item in ordered
        if item.due_at > now
    ]

    selected: list[CollectionWorkItem] = []
    selected_ids: set[str] = set()
    usage = _BudgetUsage()

    # Reserve a small configurable part of the batch for non-live work. This
    # is an item-count reservation; the normal page/time budgets still apply.
    non_live_items = [item for item in due_items if item.work_type is not WorkType.LIVE_REFRESH]
    reserved = 0
    for item in non_live_items:
        if reserved >= budget.reserved_background_items:
            break
        if _budget_block(item, usage, budget) is None:
            selected.append(item)
            selected_ids.add(item.work_id)
            usage.add(item)
            reserved += 1

    # Fill the rest in global priority order. A work item that cannot fit is
    # deferred without preventing a smaller later item from using spare budget.
    for item in due_items:
        if item.work_id in selected_ids:
            continue
        reason = _budget_block(item, usage, budget)
        if reason is None:
            selected.append(item)
            selected_ids.add(item.work_id)
            usage.add(item)
        else:
            deferred.append(DeferredWork(item, reason))

    selected.sort(key=_sort_key)
    deferred.sort(key=lambda entry: _sort_key(entry.item))
    return PlannerResult(tuple(selected), tuple(deferred))
