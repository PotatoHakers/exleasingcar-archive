#!/usr/bin/env python3
"""Read-only per-lot coverage report for one Exleasingcar auction.

Shows which cars are already archived, when API data was last checked, when
the detail page was last opened, and the latest persisted highest_offer.
The report never writes to the database or contacts the website.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


def open_read_only(path: Path) -> sqlite3.Connection:
    resolved = path.resolve().as_posix()
    uri = f"file:{quote(resolved, safe='/:')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("auction_id")
    parser.add_argument(
        "--since",
        help=(
            "Optional timezone-aware run start, e.g. "
            "2026-11-25T13:51:04+03:00. API/detail freshness is compared to this."
        ),
    )
    args = parser.parse_args()

    if not args.database.is_file():
        print(f"Database file not found: {args.database}", file=sys.stderr)
        return 2
    since = parse_datetime(args.since) if args.since else None
    if args.since and since is None:
        print("--since must be a valid ISO timestamp", file=sys.stderr)
        return 2

    try:
        connection = open_read_only(args.database)
    except sqlite3.Error as exc:
        print(f"Cannot open database read-only: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    with connection:
        rows = connection.execute(
            """
            SELECT
                v.source_car_id,
                COALESCE(
                    NULLIF(TRIM(COALESCE(v.displayed_title, '')), ''),
                    TRIM(COALESCE(v.make, '') || ' ' || COALESCE(v.model, '')),
                    'Unknown vehicle'
                ) AS vehicle,
                al.sale_type,
                al.collection_eligible,
                al.monitoring_completed_at,
                al.monitoring_completion_reason,
                al.first_seen_at,
                al.last_detail_attempted_at,
                al.last_detail_checked_at,
                al.last_api_checked_at,
                (
                    SELECT po.highest_offer
                    FROM price_observations AS po
                    WHERE po.auction_lot_id = al.id
                    ORDER BY po.observed_at DESC, po.id DESC
                    LIMIT 1
                ) AS latest_highest_offer,
                (
                    SELECT po.currency
                    FROM price_observations AS po
                    WHERE po.auction_lot_id = al.id
                    ORDER BY po.observed_at DESC, po.id DESC
                    LIMIT 1
                ) AS price_currency,
                (
                    SELECT po.observed_at
                    FROM price_observations AS po
                    WHERE po.auction_lot_id = al.id
                    ORDER BY po.observed_at DESC, po.id DESC
                    LIMIT 1
                ) AS latest_price_observed_at,
                (
                    SELECT ae.auction_phase
                    FROM auction_events AS ae
                    WHERE ae.auction_lot_id = al.id
                    ORDER BY ae.observed_at DESC, ae.id DESC
                    LIMIT 1
                ) AS latest_auction_phase,
                (
                    SELECT ae.bid_status
                    FROM auction_events AS ae
                    WHERE ae.auction_lot_id = al.id
                    ORDER BY ae.observed_at DESC, ae.id DESC
                    LIMIT 1
                ) AS latest_bid_status,
                (
                    SELECT ae.live_bidding
                    FROM auction_events AS ae
                    WHERE ae.auction_lot_id = al.id
                    ORDER BY ae.observed_at DESC, ae.id DESC
                    LIMIT 1
                ) AS latest_live_bidding,
                (
                    SELECT ae.remaining_seconds
                    FROM auction_events AS ae
                    WHERE ae.auction_lot_id = al.id
                    ORDER BY ae.observed_at DESC, ae.id DESC
                    LIMIT 1
                ) AS latest_remaining_seconds
            FROM auctions AS a
            JOIN auction_lots AS al ON al.auction_id = a.id
            JOIN vehicles AS v ON v.id = al.vehicle_id
            WHERE a.exleasingcar_auction_id = ?
            ORDER BY v.displayed_title COLLATE NOCASE, v.source_car_id
            """,
            (args.auction_id,),
        ).fetchall()

    if not rows:
        print(f"No archived lots found for auction {args.auction_id}.")
        return 0

    api_updated = 0
    detail_attempted_count = 0
    detail_checked_count = 0
    already_archived = 0
    sale_type_counts: dict[str, int] = {}
    eligible_count = 0
    completed_count = 0
    completion_reasons: dict[str, int] = {}
    print(f"Auction {args.auction_id}: {len(rows)} archived lot(s)")
    if since:
        print(f"Run start (UTC): {since.isoformat()}")
    print()

    header = (
        "Car ID | Vehicle | Sale type | Eligible | API checked | "
        "Detail attempted | Detail checked | Monitoring | "
        "Latest highest_offer | Latest API price time | Event"
    )
    print(header)
    print("-" * len(header))

    for row in rows:
        first_seen = parse_datetime(row["first_seen_at"])
        api_checked = parse_datetime(row["last_api_checked_at"])
        detail_checked = parse_datetime(row["last_detail_checked_at"])
        detail_attempted = parse_datetime(row["last_detail_attempted_at"])
        monitoring_completed = parse_datetime(row["monitoring_completed_at"])
        sale_type = row["sale_type"] or "unknown"
        sale_type_counts[sale_type] = sale_type_counts.get(sale_type, 0) + 1
        eligible_count += int(bool(row["collection_eligible"]))
        if monitoring_completed is not None:
            completed_count += 1
            reason = row["monitoring_completion_reason"] or "unspecified"
            completion_reasons[reason] = completion_reasons.get(reason, 0) + 1

        if since and first_seen and first_seen < since:
            already_archived += 1
        elif since and first_seen is None:
            already_archived += 1

        was_api_updated = bool(since and api_checked and api_checked >= since)
        was_detail_attempted = bool(
            since and detail_attempted and detail_attempted >= since
        )
        was_detail_checked = bool(
            since and detail_checked and detail_checked >= since
        )
        api_updated += int(was_api_updated)
        detail_attempted_count += int(was_detail_attempted)
        detail_checked_count += int(was_detail_checked)

        api_label = (
            "YES " + row["last_api_checked_at"]
            if was_api_updated
            else (row["last_api_checked_at"] or "never")
        )
        detail_attempted_label = (
            "YES " + row["last_detail_attempted_at"]
            if was_detail_attempted
            else (row["last_detail_attempted_at"] or "never")
        )
        detail_checked_label = (
            "YES " + row["last_detail_checked_at"]
            if was_detail_checked
            else (row["last_detail_checked_at"] or "never")
        )
        monitoring_label = (
            f'COMPLETED {row["monitoring_completed_at"]} '
            f'({row["monitoring_completion_reason"] or "unspecified"})'
            if row["monitoring_completed_at"]
            else "not completed"
        )
        event = "/".join(
            str(row[key]) if row[key] is not None else "-"
            for key in (
                "latest_auction_phase",
                "latest_bid_status",
                "latest_live_bidding",
                "latest_remaining_seconds",
            )
        )
        price = (
            f'{row["latest_highest_offer"]} {row["price_currency"] or ""}'.strip()
            if row["latest_highest_offer"] is not None
            else "not recorded"
        )
        print(
            f'{row["source_car_id"]} | {row["vehicle"]} | '
            f'{sale_type} | {row["collection_eligible"]} | '
            f'{api_label} | {detail_attempted_label} | {detail_checked_label} | '
            f'{monitoring_label} | {price} | '
            f'{row["latest_price_observed_at"] or "-"} | {event}'
        )

    print()
    print(f"Lots in this auction already archived before run: {already_archived}")
    print(
        "Sale types: "
        + ", ".join(f"{name}={count}" for name, count in sorted(sale_type_counts.items()))
    )
    print(f"Eligible lots: {eligible_count}; not eligible: {len(rows) - eligible_count}")
    print(f"Open-lot monitoring completed: {completed_count}")
    if completion_reasons:
        print(
            "Completion reasons: "
            + ", ".join(
                f"{name}={count}" for name, count in sorted(completion_reasons.items())
            )
        )
    if since:
        print(f"Distinct lots API-checked during run: {api_updated}")
        print(f"Detail attempts during run: {detail_attempted_count}")
        print(f"Successful detail checks during run: {detail_checked_count}")
        print(
            "Note: API checked can be YES even when highest_offer is unchanged; "
            "last_api_checked_at tracks freshness separately from price history. "
            "Detail attempts and successful checks are reported separately."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
