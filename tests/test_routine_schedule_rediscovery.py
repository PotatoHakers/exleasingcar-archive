from datetime import datetime, timedelta, timezone

from exleasingcar_archive.routine_schedule import (
    DEFAULT_REFRESH_INTERVAL,
    RoutineSchedule,
)

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


def test_rediscovery_does_not_pull_a_future_refresh_forward(tmp_path):
    schedule = RoutineSchedule(tmp_path / "runtime.sqlite3")
    schedule.initialize()
    schedule.register("auction-1", due_at=NOW)
    completed = NOW + timedelta(minutes=2)
    schedule.mark_succeeded("auction-1", at=completed)

    schedule.register("auction-1", due_at=NOW + timedelta(hours=1))

    entry = schedule.get("auction-1")
    assert entry.next_due_at == completed + DEFAULT_REFRESH_INTERVAL
    assert schedule.due(now=NOW + timedelta(hours=1)) == ()
