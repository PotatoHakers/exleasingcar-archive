from datetime import datetime, timedelta, timezone

import pytest

from exleasingcar_archive.scheduler import (
    CollectionWorkItem,
    DeferredReason,
    PlannerBudget,
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
        WorkType.PRESTART_REFRESH: {"catalog_pages": units},
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
        work("prestart", WorkType.PRESTART_REFRESH),
        work("bootstrap", WorkType.BOOTSTRAP),
        work("live-later", WorkType.LIVE_REFRESH, due_at=NOW - timedelta(minutes=2)),
        work("live-earlier", WorkType.LIVE_REFRESH, due_at=NOW - timedelta(minutes=8)),
    ]

    result = plan_collection_work(items, now=NOW, budget=budget())

    assert [item.work_id for item in result.selected] == [
        "live-earlier",
        "live-later",
        "bootstrap",
        "prestart",
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
    assert result.deferred == (
        # The larger first item is skipped; the smaller item still fits.
        result.deferred[0],
    )
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
    assert result.deferred == (type(result.deferred[0])(future, DeferredReason.NOT_DUE),)


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
