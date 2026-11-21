"""Show the account-wide R2 safety budget without making R2 requests."""

from __future__ import annotations

from dotenv import load_dotenv

from .r2_analytics import CloudflareR2Analytics, R2AnalyticsError
from .r2_budget import R2BudgetExceeded, R2UsageBudget


def main() -> int:
    load_dotenv()
    try:
        with CloudflareR2Analytics.from_env() as analytics:
            snapshot = analytics.fetch_usage()
        budget = R2UsageBudget.from_env(snapshot)
        report = budget.report()
    except (R2AnalyticsError, R2BudgetExceeded, RuntimeError, ValueError) as exc:
        print(f"R2 budget report unavailable: {exc}")
        print("Do not start a real R2 upload until this report succeeds.")
        return 2

    storage_used_gb = report["storage_used_bytes"] / 1_000_000_000
    storage_limit_gb = report["storage_limit_bytes"] / 1_000_000_000
    print("Cloudflare R2 account-wide safety budget")
    print(
        f"Class A operations: {report['class_a_used']:,} / "
        f"{report['class_a_limit']:,} "
        f"({report['class_a_remaining']:,} safety headroom)"
    )
    print(
        f"Class B operations: {report['class_b_used']:,} / "
        f"{report['class_b_limit']:,} "
        f"({report['class_b_remaining']:,} safety headroom)"
    )
    print(
        f"Peak storage: {storage_used_gb:.3f} / {storage_limit_gb:.3f} GB "
        f"({report['storage_remaining_bytes'] / 1_000_000_000:.3f} GB headroom)"
    )

    exhausted = (
        report["class_a_remaining"] <= 0
        or report["class_b_remaining"] <= 0
        or report["storage_remaining_bytes"] <= 0
    )
    if exhausted:
        print("R2 guard status: STOPPED — at least one safety threshold is exhausted.")
        return 1

    print("R2 guard status: READY — within the application's conservative limits.")
    print(
        "Note: this report covers the Cloudflare account's reported R2 usage. "
        "Other R2 clients in the account share the same free-tier allocation."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
