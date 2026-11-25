#!/usr/bin/env python3
"""Read-only, privacy-preserving QA summary for a real-site SQLite archive.

This script opens the archive in SQLite read-only mode. It does not contact
Exleasingcar, Cloudflare, or R2 and does not print VINs, lot/vehicle IDs, source
URLs, credentials, or browser-session data.

Usage:
    python real_site_validation_report.py data/real_site_validation.sqlite3
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

EXPECTED_SCHEMA_VERSION = 18
REQUIRED_TABLES = (
    "auctions",
    "vehicles",
    "auction_lots",
    "catalog_scan_runs",
    "catalog_scan_pages",
    "catalog_deferred_api_lots",
)

TABLES = (
    "auctions",
    "vehicles",
    "auction_lots",
    "price_observations",
    "price_reference_observations",
    "lot_price_observations",
    "auction_events",
    "lot_media_sources",
    "media_assets",
    "media_asset_versions",
    "media_derivatives",
    "pdf_field_observations",
    "pdf_damage_observations",
    "catalog_scan_runs",
    "catalog_scan_pages",
    "catalog_deferred_api_lots",
)


def open_read_only(path: Path) -> sqlite3.Connection:
    """Open an existing SQLite file without creating or modifying it."""
    resolved = path.resolve().as_posix()
    uri = f"file:{quote(resolved, safe='/:')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row["name"]) for row in rows}


def count_rows(connection: sqlite3.Connection, table: str) -> int:
    # Table names come exclusively from the fixed TABLES constant above.
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def print_media_summary(connection: sqlite3.Connection, tables: set[str]) -> None:
    print("\nMedia discovery (no URLs or IDs are displayed):")
    if "lot_media_sources" not in tables:
        print("  lot_media_sources: MISSING")
        return

    rows = connection.execute(
        """
        SELECT
            media_type,
            COUNT(*) AS source_rows,
            COUNT(DISTINCT source_url) AS distinct_source_urls,
            COUNT(DISTINCT auction_lot_id) AS lots_with_media,
            SUM(CASE WHEN download_status = 'discovered' THEN 1 ELSE 0 END)
                AS discovered,
            SUM(CASE WHEN download_status = 'downloaded' THEN 1 ELSE 0 END)
                AS downloaded,
            SUM(CASE WHEN download_status = 'failed' THEN 1 ELSE 0 END)
                AS failed,
            SUM(CASE WHEN current_asset_id IS NOT NULL THEN 1 ELSE 0 END)
                AS linked_to_archived_asset,
            SUM(CASE WHEN instr(source_url, '?') > 0 THEN 1 ELSE 0 END)
                AS urls_with_query_parameters
        FROM lot_media_sources
        GROUP BY media_type
        ORDER BY media_type
        """
    ).fetchall()

    if not rows:
        print("  No media source rows found.")
        return

    for row in rows:
        print(
            "  {kind}: source_rows={rows}, distinct_urls={urls}, lots={lots}, "
            "discovered={discovered}, downloaded={downloaded}, failed={failed}, "
            "archived_links={archived}, urls_with_query_parameters={query}".format(
                kind=row["media_type"],
                rows=row["source_rows"],
                urls=row["distinct_source_urls"],
                lots=row["lots_with_media"],
                discovered=row["discovered"],
                downloaded=row["downloaded"],
                failed=row["failed"],
                archived=row["linked_to_archived_asset"],
                query=row["urls_with_query_parameters"],
            )
        )

    by_type = {str(row["media_type"]): int(row["distinct_source_urls"]) for row in rows}
    if "photo" in by_type and "report" in by_type:
        print(
            "  Reference sample check: expected 43 photo URLs and 4 report URLs "
            "for the previously supplied sample; observed "
            f"{by_type['photo']} photo URL(s) and {by_type['report']} report URL(s)."
        )
        if by_type["photo"] != 43 or by_type["report"] != 4:
            print("  NOTE: a different auction/lot scope can legitimately produce different totals.")


def print_scheduler_summary(connection: sqlite3.Connection, tables: set[str]) -> None:
    print("\nCatalogue progress:")
    if "catalog_scan_runs" not in tables:
        print("  catalog_scan_runs: MISSING")
        return

    rows = connection.execute(
        """
        SELECT scan_type, status, COUNT(*) AS runs,
               COALESCE(SUM(pages_completed), 0) AS pages_completed
        FROM catalog_scan_runs
        GROUP BY scan_type, status
        ORDER BY scan_type, status
        """
    ).fetchall()
    if rows:
        for row in rows:
            print(
                f"  {row['scan_type']} / {row['status']}: "
                f"runs={row['runs']}, pages_completed={row['pages_completed']}"
            )
    else:
        print("  no catalogue scans recorded")

    if "catalog_scan_pages" in tables:
        page_count = count_rows(connection, "catalog_scan_pages")
        print(f"  completed page checkpoints: {page_count}")
    if "catalog_deferred_api_lots" in tables:
        pending = connection.execute(
            "SELECT COUNT(*) FROM catalog_deferred_api_lots WHERE processed_at IS NULL"
        ).fetchone()[0]
        print(f"  deferred API lot updates still pending: {pending}")


def print_price_summary(connection: sqlite3.Connection, tables: set[str]) -> None:
    print("\nPrice history summary:")
    if "auction_lots" not in tables:
        print("  auction_lots: MISSING")
        return

    lot_count = count_rows(connection, "auction_lots")
    print(f"  auction lots: {lot_count}")

    if "price_observations" in tables:
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS observations,
                COALESCE(SUM(CASE WHEN TRIM(highest_offer) IN ('0', '0.0', '0.00', '0.000')
                         THEN 1 ELSE 0 END), 0) AS explicit_zero_observations,
                COUNT(DISTINCT auction_lot_id) AS lots_with_api_price
            FROM price_observations
            """
        ).fetchone()
        without_price = connection.execute(
            """
            SELECT COUNT(*) FROM auction_lots AS lot
            WHERE NOT EXISTS (
                SELECT 1 FROM price_observations AS price
                WHERE price.auction_lot_id = lot.id
            )
            """
        ).fetchone()[0]
        print(
            f"  API highest_offer observations: {row['observations']}; "
            f"explicit-zero observations: {row['explicit_zero_observations']}; "
            f"lots with API price history: {row['lots_with_api_price']}; "
            f"lots with no API price observation: {without_price}"
        )

    if "price_reference_observations" in tables:
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS observations,
                COALESCE(SUM(CASE WHEN minimum_price IS NOT NULL THEN 1 ELSE 0 END), 0)
                    AS minimum_values,
                COALESCE(SUM(CASE WHEN indicative_price IS NOT NULL THEN 1 ELSE 0 END), 0)
                    AS indicative_values
            FROM price_reference_observations
            """
        ).fetchone()
        print(
            "  seller price references: "
            f"rows={row['observations']}, minimum values={row['minimum_values']}, "
            f"indicative values={row['indicative_values']}"
        )

    if "lot_price_observations" in tables:
        rows = connection.execute(
            """
            SELECT price_type, COUNT(*) AS observations,
                   COUNT(DISTINCT auction_lot_id) AS lots
            FROM lot_price_observations
            GROUP BY price_type
            ORDER BY price_type
            """
        ).fetchall()
        for row in rows:
            print(
                f"  {row['price_type']}: {row['observations']} observation(s) "
                f"across {row['lots']} lot(s)"
            )



def _report_value(value: object | None) -> str:
    """Format a database value on one line without exposing source identifiers."""
    if value is None:
        return "not recorded"
    text = " ".join(str(value).split())
    return text[:120] + ("…" if len(text) > 120 else "")


def print_source_observation_samples(
    connection: sqlite3.Connection, tables: set[str], *, limit: int = 3
) -> None:
    """Print a few latest source values without lot IDs, VINs, or source URLs."""
    print("\\nRecent source observations (latest samples; IDs and URLs omitted):")

    if "price_observations" in tables:
        rows = connection.execute(
            """
            SELECT highest_offer, currency, observed_at, source
            FROM price_observations
            ORDER BY observed_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        print("  Raw API highest_offer (not independently verified as a public bid):")
        if not rows:
            print("    no observations")
        for row in rows:
            print(
                "    highest_offer={value} {currency}; observed_at={observed}; "
                "source={source}".format(
                    value=_report_value(row["highest_offer"]),
                    currency=_report_value(row["currency"]),
                    observed=_report_value(row["observed_at"]),
                    source=_report_value(row["source"]),
                )
            )

    if "price_reference_observations" in tables:
        rows = connection.execute(
            """
            SELECT minimum_price, indicative_price, currency, observed_at, source
            FROM price_reference_observations
            ORDER BY observed_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        print("  Seller reference fields (not treated as public bids or Buy Now prices):")
        if not rows:
            print("    no observations")
        for row in rows:
            print(
                "    minimum_price={minimum}; indicative_price={indicative}; "
                "currency={currency}; observed_at={observed}; source={source}".format(
                    minimum=_report_value(row["minimum_price"]),
                    indicative=_report_value(row["indicative_price"]),
                    currency=_report_value(row["currency"]),
                    observed=_report_value(row["observed_at"]),
                    source=_report_value(row["source"]),
                )
            )

    if "lot_price_observations" in tables:
        rows = connection.execute(
            """
            SELECT price_type, amount, currency, source_field, observed_at, source
            FROM lot_price_observations
            ORDER BY observed_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        print("  Detail/catalogue price observations (type is kept explicit):")
        if not rows:
            print("    no observations")
        for row in rows:
            print(
                "    price_type={kind}; amount={amount} {currency}; "
                "source_field={field}; observed_at={observed}; source={source}".format(
                    kind=_report_value(row["price_type"]),
                    amount=_report_value(row["amount"]),
                    currency=_report_value(row["currency"]),
                    field=_report_value(row["source_field"]),
                    observed=_report_value(row["observed_at"]),
                    source=_report_value(row["source"]),
                )
            )

    if "auction_events" in tables:
        rows = connection.execute(
            """
            SELECT auction_phase, bid_status, final_call_active, live_bidding,
                   auction_end_at, remaining_seconds, observed_at, source
            FROM auction_events
            ORDER BY observed_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        print("  Per-lot event observations (not an auction-level start signal):")
        if not rows:
            print("    no observations")
        for row in rows:
            print(
                "    phase={phase}; bid_status={bid}; final_call_active={final}; "
                "live_bidding={live}; auction_end_at={end}; remaining_seconds={remaining}; "
                "observed_at={observed}; source={source}".format(
                    phase=_report_value(row["auction_phase"]),
                    bid=_report_value(row["bid_status"]),
                    final=_report_value(row["final_call_active"]),
                    live=_report_value(row["live_bidding"]),
                    end=_report_value(row["auction_end_at"]),
                    remaining=_report_value(row["remaining_seconds"]),
                    observed=_report_value(row["observed_at"]),
                    source=_report_value(row["source"]),
                )
            )

    print(
        "  Auction-level scheduled start: not stored in schema v18; "
        "per-lot end/countdown fields are not a substitute."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="Path to the SQLite archive database")
    args = parser.parse_args()

    if not args.database.is_file():
        print(f"Database file not found: {args.database}", file=sys.stderr)
        return 2

    try:
        connection = open_read_only(args.database)
    except sqlite3.Error as exc:
        print(f"Cannot open database read-only: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    errors = 0
    with connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        tables = table_names(connection)

        print("Exleasingcar Archive — local real-site validation report")
        print(f"Database file: {args.database.name}")
        print(f"SQLite schema version: {version} (expected current: {EXPECTED_SCHEMA_VERSION})")
        print(f"SQLite integrity_check: {integrity}")
        print(f"Foreign-key violations: {len(foreign_key_errors)}")

        if version != EXPECTED_SCHEMA_VERSION:
            print("  ERROR: database schema is not at the current application version.")
            errors += 1
        if integrity != "ok":
            errors += 1
        if foreign_key_errors:
            errors += 1

        print("\nTable row counts:")
        for table in TABLES:
            if table in tables:
                print(f"  {table}: {count_rows(connection, table)}")
            else:
                print(f"  {table}: not present")
                if table in REQUIRED_TABLES:
                    errors += 1

        print_media_summary(connection, tables)
        print_price_summary(connection, tables)
        print_source_observation_samples(connection, tables)
        print_scheduler_summary(connection, tables)

        if "auction_lots" in tables:
            coverage = connection.execute(
                """
                SELECT
                    COUNT(*) AS total_lots,
                    COALESCE(SUM(CASE WHEN last_detail_attempted_at IS NOT NULL THEN 1 ELSE 0 END), 0)
                        AS detail_attempted,
                    COALESCE(SUM(CASE WHEN last_detail_checked_at IS NOT NULL THEN 1 ELSE 0 END), 0)
                        AS detail_checked,
                    COALESCE(SUM(CASE WHEN last_api_checked_at IS NOT NULL THEN 1 ELSE 0 END), 0)
                        AS api_checked
                FROM auction_lots
                """
            ).fetchone()
            print("\nCollector coverage:")
            print(
                f"  lots={coverage['total_lots']}, "
                f"detail_attempted={coverage['detail_attempted']}, "
                f"detail_checked={coverage['detail_checked']}, "
                f"api_checked={coverage['api_checked']}"
            )

    print("\nResult: " + ("CHECK FAILED" if errors else "STRUCTURAL CHECKS PASSED"))
    print("This report is local-only and performs no website or Cloudflare requests.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
