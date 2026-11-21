from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
import pytest

from exleasingcar_archive.r2_analytics import (
    CloudflareR2Analytics,
    R2AnalyticsError,
)
from exleasingcar_archive.r2_budget import (
    R2BudgetExceeded,
    R2BudgetLimits,
    R2UsageBudget,
    R2UsageSnapshot,
)


def _budget(tmp_path, *, class_a=10, class_b=10, storage=100, snapshot=None):
    return R2UsageBudget(
        tmp_path / "r2-budget.sqlite3",
        snapshot=snapshot or R2UsageSnapshot(0, 0, 0),
        limits=R2BudgetLimits(class_a, class_b, storage),
    )


def test_operation_budget_is_reserved_before_the_request_and_persists(tmp_path):
    path = tmp_path / "r2-budget.sqlite3"
    budget = R2UsageBudget(
        path,
        snapshot=R2UsageSnapshot(0, 0, 0),
        limits=R2BudgetLimits(2, 2, 100),
    )

    budget.reserve_operation("class_a")
    budget.reserve_operation("class_a")
    with pytest.raises(R2BudgetExceeded, match="Class A"):
        budget.reserve_operation("class_a")

    reopened = R2UsageBudget(
        path,
        snapshot=R2UsageSnapshot(0, 0, 0),
        limits=R2BudgetLimits(2, 2, 100),
    )
    assert reopened.report()["class_a_used"] == 2
    with pytest.raises(R2BudgetExceeded, match="threshold"):
        reopened.ensure_available()


def test_unknown_operations_consume_both_operation_budgets(tmp_path):
    budget = _budget(tmp_path, class_a=1, class_b=1)

    budget.reserve_operation("unknown")
    with pytest.raises(R2BudgetExceeded, match="Class B"):
        budget.reserve_operation("class_b")

    report = budget.report()
    assert report["class_a_used"] == 1
    assert report["class_b_used"] == 1


def test_upload_reservation_is_content_keyed_and_limits_storage(tmp_path):
    budget = _budget(tmp_path, class_a=3, storage=10)

    budget.reserve_put("archive/a", 6)
    budget.reserve_put("archive/a", 6)

    with pytest.raises(R2BudgetExceeded, match="storage"):
        budget.reserve_put("archive/b", 5)

    report = budget.report()
    assert report["class_a_used"] == 2
    assert report["storage_used_bytes"] == 6


def test_account_storage_snapshot_stops_all_r2_operations(tmp_path):
    budget = _budget(
        tmp_path,
        class_a=10,
        class_b=10,
        storage=10,
        snapshot=R2UsageSnapshot(0, 0, 10),
    )

    with pytest.raises(R2BudgetExceeded, match="storage"):
        budget.reserve_operation("class_b")


def test_account_analytics_usage_is_combined_conservatively():
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload = {
        "data": {
            "viewer": {
                "accounts": [
                    {
                        "r2OperationsAdaptiveGroups": [
                            {"sum": {"requests": 5}, "dimensions": {"actionType": "PutObject"}},
                            {"sum": {"requests": 8}, "dimensions": {"actionType": "GetObject"}},
                            {"sum": {"requests": 4}, "dimensions": {"actionType": "FutureOperation"}},
                            {"sum": {"requests": 10}, "dimensions": {"actionType": "DeleteObject"}},
                        ],
                        "r2StorageAdaptiveGroups": [
                            {
                                "max": {"payloadSize": 1024, "metadataSize": 100},
                                "dimensions": {"datetime": now, "bucketName": "bucket-one"},
                            },
                            {
                                "max": {"payloadSize": 2048, "metadataSize": 200},
                                "dimensions": {"datetime": now, "bucketName": "bucket-two"},
                            },
                        ],
                    }
                ]
            }
        }
    }

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload)
        )
    )
    with CloudflareR2Analytics("a" * 32, "read-only-token", client=client) as analytics:
        snapshot = analytics.fetch_usage()
    client.close()

    assert snapshot.class_a_requests == 9
    assert snapshot.class_b_requests == 12
    assert snapshot.storage_peak_bytes == 3372


def test_analytics_failure_is_not_treated_as_zero_usage():
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(403, json={"errors": ["forbidden"]})
        )
    )
    with CloudflareR2Analytics("a" * 32, "read-only-token", client=client) as analytics:
        with pytest.raises(R2AnalyticsError, match="unavailable"):
            analytics.fetch_usage()
    client.close()


def test_analytics_environment_requires_a_separate_read_only_token():
    with pytest.raises(RuntimeError, match="EXLEASINGCAR_CF_ACCOUNT_ANALYTICS_TOKEN"):
        CloudflareR2Analytics.from_env(
            {
                "EXLEASINGCAR_R2_ACCOUNT_ID": "a" * 32,
            }
        )


def test_limits_cannot_be_raised_above_the_safe_defaults():
    with pytest.raises(ValueError, match="safe ceiling"):
        R2BudgetLimits(class_a_requests=500_001)

    with pytest.raises(ValueError, match="safe ceiling"):
        R2BudgetLimits(class_b_requests=5_000_001)


def test_parallel_reservations_never_cross_the_limit(tmp_path):
    budget_path = tmp_path / "shared-budget.sqlite3"
    budgets = [
        R2UsageBudget(
            budget_path,
            snapshot=R2UsageSnapshot(0, 0, 0),
            limits=R2BudgetLimits(8, 20, 100),
        )
        for _ in range(16)
    ]

    def reserve(item):
        try:
            item.reserve_operation("class_a")
            return True
        except R2BudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=16) as pool:
        accepted = list(pool.map(reserve, budgets))

    assert sum(accepted) == 8
    assert budgets[0].report()["class_a_used"] == 8



def test_account_analytics_with_no_storage_metrics_fails_closed():
    payload = {
        "data": {
            "viewer": {
                "accounts": [
                    {
                        "r2OperationsAdaptiveGroups": [],
                        "r2StorageAdaptiveGroups": [],
                    }
                ]
            }
        }
    }
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload)
        )
    )
    with CloudflareR2Analytics("a" * 32, "read-only-token", client=client) as analytics:
        with pytest.raises(R2AnalyticsError, match="no R2 storage metrics"):
            analytics.fetch_usage()
    client.close()


def test_stale_storage_analytics_fails_closed():
    from datetime import timedelta

    stale_timestamp = (
        datetime.now(timezone.utc) - timedelta(days=4)
    ).isoformat().replace("+00:00", "Z")
    payload = {
        "data": {
            "viewer": {
                "accounts": [
                    {
                        "r2OperationsAdaptiveGroups": [],
                        "r2StorageAdaptiveGroups": [
                            {
                                "max": {"payloadSize": 0, "metadataSize": 0},
                                "dimensions": {
                                    "datetime": stale_timestamp,
                                    "bucketName": "bucket-one",
                                },
                            }
                        ],
                    }
                ]
            }
        }
    }
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload)
        )
    )
    with CloudflareR2Analytics("a" * 32, "read-only-token", client=client) as analytics:
        with pytest.raises(R2AnalyticsError, match="older than 72 hours"):
            analytics.fetch_usage()
    client.close()


def test_real_budget_configuration_rejects_an_in_memory_ledger():
    with pytest.raises(ValueError, match="persistent file"):
        R2UsageBudget.from_env(
            R2UsageSnapshot(0, 0, 0),
            {
                "EXLEASINGCAR_R2_BUDGET_DATABASE": ":memory:",
            },
        )
