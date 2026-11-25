from datetime import datetime, timedelta, timezone

import pytest

from exleasingcar_archive.scheduler import (
    CollectionWorkItem,
    DeferredReason,
    DeferredWork,
    PlannerBudget,
    RunBudget,
    RunBudgetReason,
    RunLimits,
    WorkType,
    plan_collection_work,
)

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def work(
    work_id: str,
    work_type: WorkType,
    *,
    due_at: datetime = NOW - timedelta(minutes=5),
    auction_id: str = "auction-1",
    target_id: str | None = None,
    units: int = 1,
    estimated_seconds: int = 1,
) -> CollectionWorkItem:
    costs = {
        WorkType.LIVE_REFRESH: {"live_lots": units},
        WorkType.DETAIL_ENRICHMENT: {"detail_pages": units},
        WorkType.BOOTSTRAP: {"catalog_pages": units},
        WorkType.SCHEDULED_END_REFRESH: {"catalog_pages": units},
        WorkType.ROUTINE_REFRESH: {"catalog_pages": units},
    }[work_type]
    return CollectionWorkItem(
        work_id=work_id,
        work_type=work_type,
        auction_id=auction_id,
        due_at=due_at,
        target_id=target_id,
        estimated_seconds=estimated_seconds,
        **costs,
    )


def budget(**overrides) -> PlannerBudget:
    values = {
        "max_items": 10,
        "max_estimated_seconds": 60,
        "max_catalog_pages": 10,
        "max_live_lots": 100,
        "max_detail_pages": 10,
        "reserved_background_items": 1,
    }
    values.update(overrides)
    return PlannerBudget(**values)


def test_plan_uses_owner_priority_and_stable_due_order():
    items = [
        work("details", WorkType.DETAIL_ENRICHMENT),
        work("routine", WorkType.ROUTINE_REFRESH),
        work("scheduled-end", WorkType.SCHEDULED_END_REFRESH),
        work("bootstrap", WorkType.BOOTSTRAP),
        work("live-later", WorkType.LIVE_REFRESH, due_at=NOW - timedelta(minutes=2)),
        work("live-earlier", WorkType.LIVE_REFRESH, due_at=NOW - timedelta(minutes=8)),
    ]

    result = plan_collection_work(items, now=NOW, budget=budget())

    assert [item.work_id for item in result.selected] == [
        "live-earlier",
        "live-later",
        "bootstrap",
        "scheduled-end",
        "routine",
        "details",
    ]
    assert result.deferred == ()


def test_background_reservation_prevents_live_work_from_starving_other_tasks():
    items = [
        work("live-1", WorkType.LIVE_REFRESH),
        work("live-2", WorkType.LIVE_REFRESH),
        work("live-3", WorkType.LIVE_REFRESH),
        work("bootstrap", WorkType.BOOTSTRAP),
    ]

    result = plan_collection_work(
        items,
        now=NOW,
        budget=budget(max_items=2, reserved_background_items=1),
    )

    assert {item.work_id for item in result.selected} == {"bootstrap", "live-1"}
    assert {
        entry.item.work_id: entry.reason for entry in result.deferred
    } == {
        "live-2": DeferredReason.MAX_ITEMS,
        "live-3": DeferredReason.MAX_ITEMS,
    }


def test_zero_background_reservation_keeps_strict_live_priority():
    items = [
        work("bootstrap", WorkType.BOOTSTRAP),
        work("live", WorkType.LIVE_REFRESH),
    ]

    result = plan_collection_work(
        items,
        now=NOW,
        budget=budget(max_items=1, reserved_background_items=0),
    )

    assert [item.work_id for item in result.selected] == ["live"]
    assert result.deferred[0].reason is DeferredReason.MAX_ITEMS


def test_each_resource_budget_is_enforced_independently():
    items = [
        work("live", WorkType.LIVE_REFRESH, units=3),
        work("bootstrap", WorkType.BOOTSTRAP, units=2),
        work("details", WorkType.DETAIL_ENRICHMENT, units=1),
    ]

    result = plan_collection_work(
        items,
        now=NOW,
        budget=budget(
            max_live_lots=2,
            max_catalog_pages=1,
            max_detail_pages=0,
            reserved_background_items=0,
        ),
    )

    assert result.selected == ()
    assert {
        entry.item.work_id: entry.reason for entry in result.deferred
    } == {
        "live": DeferredReason.MAX_LIVE_LOTS,
        "bootstrap": DeferredReason.MAX_CATALOG_PAGES,
        "details": DeferredReason.MAX_DETAIL_PAGES,
    }


def test_estimated_runtime_budget_does_not_allow_a_later_item_to_be_overrun():
    items = [
        work("long", WorkType.LIVE_REFRESH, estimated_seconds=8),
        work("short", WorkType.LIVE_REFRESH, estimated_seconds=3),
    ]

    result = plan_collection_work(
        items,
        now=NOW,
        budget=budget(
            max_estimated_seconds=5,
            max_items=2,
            reserved_background_items=0,
        ),
    )

    assert [item.work_id for item in result.selected] == ["short"]
    # The larger first item is skipped; the smaller item still fits.
    assert len(result.deferred) == 1
    assert result.deferred[0].item.work_id == "long"
    assert result.deferred[0].reason is DeferredReason.MAX_ESTIMATED_SECONDS


def test_future_work_is_returned_as_not_due_and_does_not_consume_reserve():
    future = work(
        "future-bootstrap",
        WorkType.BOOTSTRAP,
        due_at=NOW + timedelta(hours=2),
    )
    live = work("live", WorkType.LIVE_REFRESH)

    result = plan_collection_work([future, live], now=NOW, budget=budget(max_items=1))

    assert [item.work_id for item in result.selected] == ["live"]
    assert result.deferred == (DeferredWork(future, DeferredReason.NOT_DUE),)


def test_ties_are_broken_by_auction_and_target_ids():
    items = [
        work("b-lot", WorkType.LIVE_REFRESH, auction_id="auction-b", target_id="lot-2"),
        work("a-lot-2", WorkType.LIVE_REFRESH, auction_id="auction-a", target_id="lot-2"),
        work("a-lot-1", WorkType.LIVE_REFRESH, auction_id="auction-a", target_id="lot-1"),
    ]

    result = plan_collection_work(
        items,
        now=NOW,
        budget=budget(reserved_background_items=0),
    )

    assert [item.work_id for item in result.selected] == [
        "a-lot-1",
        "a-lot-2",
        "b-lot",
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_items": 0},
        {"max_estimated_seconds": 0},
        {"max_catalog_pages": -1},
        {"max_live_lots": -1},
        {"max_detail_pages": -1},
        {"reserved_background_items": -1},
        {"max_items": 1, "reserved_background_items": 2},
    ],
)
def test_invalid_budgets_are_rejected(kwargs):
    values = {
        "max_items": 10,
        "max_estimated_seconds": 60,
        "max_catalog_pages": 10,
        "max_live_lots": 10,
        "max_detail_pages": 10,
        "reserved_background_items": 1,
    }
    values.update(kwargs)
    with pytest.raises(ValueError):
        PlannerBudget(**values)


def test_naive_datetime_and_duplicate_work_ids_are_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        work("naive", WorkType.LIVE_REFRESH, due_at=datetime(2026, 11, 24, 12, 0))

    repeated = work("duplicate", WorkType.LIVE_REFRESH)
    with pytest.raises(ValueError, match="unique"):
        plan_collection_work(
            [repeated, repeated],
            now=NOW,
            budget=budget(),
        )


def test_work_item_rejects_mismatched_costs():
    with pytest.raises(ValueError, match="cannot consume unrelated"):
        CollectionWorkItem(
            work_id="bad",
            work_type=WorkType.LIVE_REFRESH,
            auction_id="auction-1",
            due_at=NOW,
            live_lots=1,
            catalog_pages=1,
        )



class FakeMonotonicClock:
    def __init__(self, initial: float = 100.0):
        self.current = initial

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


def test_run_budget_tracks_catalogue_live_and_detail_units_atomically():
    clock = FakeMonotonicClock()
    run_budget = RunBudget(
        RunLimits(
            max_duration_seconds=30,
            max_catalog_pages=2,
            max_live_lots=3,
            max_detail_pages=1,
        ),
        monotonic_clock=clock,
    )

    assert run_budget.try_consume(catalog_pages=1, live_lots=2) is None
    assert run_budget.try_consume(catalog_pages=1, detail_pages=1) is None

    snapshot = run_budget.snapshot()
    assert snapshot.catalog_pages_used == 2
    assert snapshot.live_lots_used == 2
    assert snapshot.detail_pages_used == 1
    assert snapshot.elapsed_seconds == 0
    assert snapshot.remaining_seconds == 30
    assert snapshot.expired is False

    # Each exhausted resource returns its own reason without changing counters.
    assert run_budget.try_consume(catalog_pages=1) is RunBudgetReason.MAX_CATALOG_PAGES
    assert run_budget.try_consume(live_lots=2) is RunBudgetReason.MAX_LIVE_LOTS
    assert run_budget.try_consume(detail_pages=1) is RunBudgetReason.MAX_DETAIL_PAGES

    after_rejections = run_budget.snapshot()
    assert after_rejections.catalog_pages_used == 2
    assert after_rejections.live_lots_used == 2
    assert after_rejections.detail_pages_used == 1


def test_run_budget_enforces_monotonic_wall_clock_deadline():
    clock = FakeMonotonicClock(250.0)
    run_budget = RunBudget(
        RunLimits(
            max_duration_seconds=5,
            max_catalog_pages=10,
            max_live_lots=10,
            max_detail_pages=10,
        ),
        monotonic_clock=clock,
    )

    assert run_budget.try_consume(catalog_pages=1) is None
    clock.advance(4.9)
    assert run_budget.try_consume(live_lots=1) is None
    clock.advance(0.1)

    assert run_budget.try_consume(detail_pages=1) is RunBudgetReason.WALL_CLOCK
    snapshot = run_budget.snapshot()
    assert snapshot.elapsed_seconds == pytest.approx(5.0)
    assert snapshot.remaining_seconds == 0
    assert snapshot.expired is True
    assert snapshot.catalog_pages_used == 1
    assert snapshot.live_lots_used == 1
    assert snapshot.detail_pages_used == 0


def test_run_budget_rejection_does_not_partially_consume_combined_request():
    clock = FakeMonotonicClock()
    run_budget = RunBudget(
        RunLimits(
            max_duration_seconds=10,
            max_catalog_pages=1,
            max_live_lots=0,
            max_detail_pages=1,
        ),
        monotonic_clock=clock,
    )

    assert (
        run_budget.try_consume(catalog_pages=1, live_lots=1)
        is RunBudgetReason.MAX_LIVE_LOTS
    )
    assert run_budget.snapshot().catalog_pages_used == 0
    assert run_budget.snapshot().live_lots_used == 0
    assert run_budget.snapshot().detail_pages_used == 0

    # The failed request above must not have consumed its otherwise-valid page.
    assert run_budget.try_consume(catalog_pages=1) is None
    assert run_budget.try_consume(detail_pages=1) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"catalog_pages": -1},
        {"live_lots": -1},
        {"detail_pages": -1},
        {"catalog_pages": True},
        {"live_lots": 1.5},
        {},
    ],
)
def test_run_budget_rejects_invalid_or_empty_consumption(kwargs):
    run_budget = RunBudget(
        RunLimits(
            max_duration_seconds=10,
            max_catalog_pages=10,
            max_live_lots=10,
            max_detail_pages=10,
        ),
        monotonic_clock=FakeMonotonicClock(),
    )

    error = TypeError if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in kwargs.values()
    ) else ValueError
    with pytest.raises(error):
        run_budget.try_consume(**kwargs)


@pytest.mark.parametrize(
    "duration",
    [0, -1, float("nan"), float("inf"), float("-inf"), True],
)
def test_run_limits_reject_invalid_durations(duration):
    with pytest.raises((TypeError, ValueError)):
        RunLimits(
            max_duration_seconds=duration,
            max_catalog_pages=1,
            max_live_lots=1,
            max_detail_pages=1,
        )


def test_run_budget_rejects_a_clock_that_moves_backwards():
    clock = FakeMonotonicClock(100)
    run_budget = RunBudget(
        RunLimits(
            max_duration_seconds=10,
            max_catalog_pages=1,
            max_live_lots=1,
            max_detail_pages=1,
        ),
        monotonic_clock=clock,
    )
    clock.advance(1)
    assert run_budget.snapshot().elapsed_seconds == 1
    clock.current = 100.5

    with pytest.raises(ValueError, match="moved backwards"):
        run_budget.try_consume(catalog_pages=1)


def test_run_budget_rejects_non_finite_clock_values():
    class InvalidClock:
        def __init__(self):
            self.value = 100.0

        def __call__(self):
            return self.value

    clock = InvalidClock()
    run_budget = RunBudget(
        RunLimits(
            max_duration_seconds=10,
            max_catalog_pages=1,
            max_live_lots=1,
            max_detail_pages=1,
        ),
        monotonic_clock=clock,
    )
    clock.value = float("nan")

    with pytest.raises(ValueError, match="finite"):
        run_budget.snapshot()


def test_run_budget_requires_valid_limits_and_clock():
    with pytest.raises(TypeError, match="RunLimits"):
        RunBudget(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="callable"):
        RunBudget(
            RunLimits(10, 1, 1, 1),
            monotonic_clock=None,  # type: ignore[arg-type]
        )


def test_run_budget_can_disable_only_the_wall_clock_limit():
    now = [100.0]
    budget = RunBudget(
        RunLimits(
            max_duration_seconds=None,
            max_catalog_pages=2,
            max_live_lots=3,
            max_detail_pages=4,
        ),
        monotonic_clock=lambda: now[0],
    )

    now[0] += 24 * 60 * 60
    snapshot = budget.snapshot()

    assert snapshot.expired is False
    assert snapshot.remaining_seconds is None
    assert snapshot.elapsed_seconds == 24 * 60 * 60
    assert budget.try_consume(catalog_pages=1) is None
    assert budget.try_consume(live_lots=3) is None
    assert budget.try_consume(live_lots=1) is RunBudgetReason.MAX_LIVE_LOTS
    assert budget.try_consume(detail_pages=4) is None
    assert budget.try_consume(detail_pages=1) is RunBudgetReason.MAX_DETAIL_PAGES
