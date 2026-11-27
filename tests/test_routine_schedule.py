from datetime import datetime, timedelta, timezone

import pytest

from exleasingcar_archive.routine_schedule import (
    DEFAULT_REFRESH_INTERVAL,
    MAX_RETRY_DELAY,
    RoutineSchedule,
)

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


def test_schedule_survives_reopen_and_orders_due_auctions(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    schedule = RoutineSchedule(path)
    schedule.initialize()
    schedule.register("auction-b", due_at=NOW - timedelta(hours=2))
    schedule.register("auction-a", due_at=NOW - timedelta(hours=2))
    schedule.register("future", due_at=NOW + timedelta(minutes=1))

    reopened = RoutineSchedule(path)
    assert [entry.auction_id for entry in reopened.due(now=NOW)] == [
        "auction-a",
        "auction-b",
    ]


def test_register_does_not_move_an_existing_due_time_later(tmp_path):
    schedule = RoutineSchedule(tmp_path / "runtime.sqlite3")
    schedule.initialize()
    earlier = NOW - timedelta(hours=1)
    schedule.register("auction-1", due_at=earlier)
    schedule.register("auction-1", due_at=NOW + timedelta(hours=12))

    assert schedule.get("auction-1").next_due_at == earlier


def test_success_sets_twelve_hour_cadence_and_resets_failures(tmp_path):
    schedule = RoutineSchedule(tmp_path / "runtime.sqlite3")
    schedule.initialize()
    schedule.register("auction-1", due_at=NOW)
    schedule.mark_started("auction-1", at=NOW)
    schedule.mark_failed("auction-1", at=NOW, error="temporary")
    completed = NOW + timedelta(minutes=3)
    schedule.mark_succeeded("auction-1", at=completed)

    entry = schedule.get("auction-1")
    assert entry.last_attempted_at == NOW
    assert entry.last_success_at == completed
    assert entry.next_due_at == completed + DEFAULT_REFRESH_INTERVAL
    assert entry.consecutive_failures == 0
    assert entry.last_error is None


def test_failures_use_bounded_exponential_backoff_without_success_stamp(tmp_path):
    schedule = RoutineSchedule(tmp_path / "runtime.sqlite3")
    schedule.initialize()
    schedule.register("auction-1", due_at=NOW)

    delays = [
        schedule.mark_failed("auction-1", at=NOW, error="network"),
        schedule.mark_failed("auction-1", at=NOW, error="network"),
        schedule.mark_failed("auction-1", at=NOW, error="network"),
        schedule.mark_failed("auction-1", at=NOW, error="network"),
        schedule.mark_failed("auction-1", at=NOW, error="network"),
        schedule.mark_failed("auction-1", at=NOW, error="network"),
        schedule.mark_failed("auction-1", at=NOW, error="network"),
        schedule.mark_failed("auction-1", at=NOW, error="network"),
    ]

    assert delays[:4] == [
        timedelta(minutes=5),
        timedelta(minutes=10),
        timedelta(minutes=20),
        timedelta(minutes=40),
    ]
    assert delays[-1] == MAX_RETRY_DELAY
    entry = schedule.get("auction-1")
    assert entry.last_success_at is None
    assert entry.consecutive_failures == 8
    assert entry.next_due_at == NOW + MAX_RETRY_DELAY
    assert entry.last_error == "network"


def test_invalid_inputs_and_unknown_auction_are_rejected(tmp_path):
    schedule = RoutineSchedule(tmp_path / "runtime.sqlite3")
    schedule.initialize()

    with pytest.raises(ValueError, match="timezone-aware"):
        schedule.register("auction-1", due_at=datetime(2026, 11, 27, 12, 0))
    with pytest.raises(ValueError, match="non-empty"):
        schedule.register(" ", due_at=NOW)
    with pytest.raises(ValueError, match="positive integer"):
        schedule.due(now=NOW, limit=0)
    with pytest.raises(KeyError, match="unknown auction"):
        schedule.mark_started("missing", at=NOW)
    with pytest.raises(KeyError, match="unknown auction"):
        schedule.mark_succeeded("missing", at=NOW)
