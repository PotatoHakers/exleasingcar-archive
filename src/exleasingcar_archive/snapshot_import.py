"""Command-line importer for user-saved Exleasingcar snapshots."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from .database import ArchiveDatabase
from .metadata_ingestion import import_html_snapshots


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise SystemExit(f"Cannot read {path}: {exc}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Import saved Exleasingcar HTML pages into SQLite. "
            "No login or network request is performed."
        )
    )
    parser.add_argument("--auction-list", required=True, type=Path)
    parser.add_argument(
        "--catalog",
        dest="catalog_pages",
        required=True,
        action="append",
        type=Path,
        help="Saved catalogue HTML page; repeat once for each pagination page.",
    )
    parser.add_argument("--vehicle-detail", required=True, type=Path)
    parser.add_argument("--api-response", type=Path)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/exleasingcar_archive.sqlite3"),
    )
    args = parser.parse_args()

    database = ArchiveDatabase(args.database)
    database.initialize()

    catalog_texts = [_read_text(path) for path in args.catalog_pages]
    summary = import_html_snapshots(
        database,
        auction_list_html=_read_text(args.auction_list),
        catalog_html=catalog_texts[0],
        additional_catalog_htmls=catalog_texts[1:],
        vehicle_detail_html=_read_text(args.vehicle_detail),
        api_response=_read_text(args.api_response) if args.api_response else None,
    )

    with sqlite3.connect(database.path) as connection:
        counts = {
            table: connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in (
                "auctions",
                "vehicles",
                "auction_lots",
                "price_observations",
                "price_reference_observations",
                "auction_events",
            )
        }
        stored_vehicle = connection.execute(
            """
            SELECT source_car_id, make, displayed_title, chassis_number,
                   chassis_number_fragment, chassis_number_masked, location_text,
                   last_registration_country_name
            FROM vehicles WHERE source_car_id = ?
            """,
            (summary.selected_car_id,),
        ).fetchone()
        stored_lot = connection.execute(
            """
            SELECT mileage_km FROM auction_lots
            WHERE vehicle_id = (
                SELECT id FROM vehicles WHERE source_car_id = ?
            ) AND auction_id = (
                SELECT id FROM auctions WHERE exleasingcar_auction_id = ?
            )
            """,
            (summary.selected_car_id, summary.selected_auction_id),
        ).fetchone()
        schema_version = connection.execute(
            "PRAGMA user_version"
        ).fetchone()[0]

    print("HTML snapshot import completed")
    print(f"Schema version: {schema_version}")
    print(f"Selected auction: {summary.selected_auction_id}")
    print(f"Selected vehicle: {summary.selected_car_id}")
    print(f"Auction cards parsed: {summary.auctions_seen}")
    print(f"Catalogue pages imported: {summary.catalog_pages_imported}")
    print(f"Catalogue lots parsed: {summary.lots_seen}")
    if summary.missing_catalog_page_urls:
        print("WARNING: catalogue snapshot is incomplete; linked pages not supplied:")
        for missing_url in summary.missing_catalog_page_urls:
            print(f"  {missing_url}")
    print("Database row counts:")
    for table, count in counts.items():
        print(f"  {table}: {count}")
    print(f"Stored vehicle metadata: {tuple(stored_vehicle) if stored_vehicle else None}")
    print(f"Stored lot mileage (km): {stored_lot[0] if stored_lot else None}")
    if args.api_response is None:
        print("Price observations: not imported (no API response file supplied)")
    else:
        print(f"API cars processed: {summary.api_ingestion.cars_processed}")
        print(f"New prices inserted: {summary.api_ingestion.prices_inserted}")
        print(
            "New seller-price reference observations inserted: "
            f"{summary.api_ingestion.price_references_inserted}"
        )
        print(f"New auction events inserted: {summary.api_ingestion.events_inserted}")


if __name__ == "__main__":
    main()
