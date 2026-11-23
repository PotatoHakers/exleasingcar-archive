from datetime import datetime, timedelta, timezone

import pytest

from exleasingcar_archive.auction_timing import project_remaining_seconds


OBSERVED_AT = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


def test_countdown_is_projected_locally_without_network_access():
    assert project_remaining_seconds(
        90,
        OBSERVED_AT,
        as_of=OBSERVED_AT + timedelta(seconds=25),
    ) == 65


def test_projected_countdown_never_goes_below_zero():
    assert project_remaining_seconds(
        10,
        OBSERVED_AT,
        as_of=OBSERVED_AT + timedelta(seconds=30),
    ) == 0


def test_missing_timer_stays_unknown():
    assert project_remaining_seconds(None, OBSERVED_AT) is None
    assert project_remaining_seconds(90, None) is None


def test_stale_or_future_observation_is_not_projected():
    assert project_remaining_seconds(
        90,
        OBSERVED_AT,
        as_of=OBSERVED_AT + timedelta(seconds=301),
    ) is None
    assert project_remaining_seconds(
        90,
        OBSERVED_AT,
        as_of=OBSERVED_AT - timedelta(seconds=1),
    ) is None


def test_countdown_projection_rejects_naive_timestamps():
    with pytest.raises(ValueError, match="timezone-aware"):
        project_remaining_seconds(
            90,
            datetime(2026, 11, 23, 12, 0),
            as_of=OBSERVED_AT,
        )
