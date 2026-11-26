import sqlite3
import subprocess
import sys
from pathlib import Path

from exleasingcar_archive.database import SCHEMA_VERSION, ArchiveDatabase

ROOT = Path(__file__).resolve().parents[1]
REPORT_SCRIPT = ROOT / "real_site_validation_report.py"


def run_report(database_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPORT_SCRIPT), str(database_path)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )


def test_validation_report_accepts_current_schema_and_summarizes_scan_tables(tmp_path):
    database = ArchiveDatabase(tmp_path / "validation.sqlite3")
    database.initialize()

    result = run_report(database.path)

    assert result.returncode == 0, result.stderr + result.stdout
    assert f"SQLite schema version: {SCHEMA_VERSION}" in result.stdout
    assert "catalog_scan_runs:" in result.stdout
    assert "catalog_scan_pages:" in result.stdout
    assert "catalog_deferred_api_lots:" in result.stdout
    assert "auction_discovery_runs:" in result.stdout
    assert "auction_discovery_pages:" in result.stdout
    assert "auction_bootstrap_queue:" in result.stdout
    assert "Global auction bootstrap:" in result.stdout
    assert "Bootstrap queue:" in result.stdout
    assert "Catalogue progress:" in result.stdout
    assert "STRUCTURAL CHECKS PASSED" in result.stdout


def test_validation_report_summarizes_discovery_and_bootstrap_without_urls_or_ids(
    tmp_path,
):
    from datetime import datetime, timedelta, timezone

    from exleasingcar_archive.database import SCHEMA_VERSION

    database = ArchiveDatabase(tmp_path / "global-bootstrap-report.sqlite3")
    database.initialize()
    first_page = "https://www.exleasingcar.com/ru/auto-auctions"
    auction_url = "https://www.exleasingcar.com/ru/auction-private-id"
    now = datetime(2026, 11, 26, 10, 0, tzinfo=timezone.utc)
    discovery = database.start_or_resume_auction_discovery(
        first_page, observed_at=now
    )
    database.record_auction_discovery_page(
        discovery.run_id,
        1,
        first_page,
        next_page_url=None,
        next_page_number=None,
        auction_count=1,
        observed_at=now,
    )
    database.upsert_auction("private-auction-id", source_url=auction_url)
    database.enqueue_bootstrap_auction(
        "private-auction-id", observed_at=now
    )
    database.mark_bootstrap_auction_started(
        "private-auction-id", observed_at=now + timedelta(seconds=1)
    )
    database.upsert_auction("private-auction-without-link")
    database.enqueue_bootstrap_auction(
        "private-auction-without-link", observed_at=now
    )

    result = run_report(database.path)

    assert result.returncode == 0, result.stderr + result.stdout
    assert f"SQLite schema version: {SCHEMA_VERSION}" in result.stdout
    assert "auction-list discovery: completed; pages=1; auction-card appearances=1" in result.stdout
    assert (
        "bootstrap queue: total=2; completed=0; in_progress=1; "
        "pending=0; deferred=0; blocked_no_source_url=1"
    ) in result.stdout
    assert "private-auction-id" not in result.stdout
    assert "private-auction-without-link" not in result.stdout
    assert auction_url not in result.stdout
    assert first_page not in result.stdout


def test_validation_report_fails_closed_for_an_old_schema(tmp_path):
    database_path = tmp_path / "old.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version = 13")

    result = run_report(database_path)

    assert result.returncode == 1
    assert "expected current: 19" in result.stdout
    assert "CHECK FAILED" in result.stdout

def test_validation_report_shows_safe_source_samples_without_identifiers(tmp_path):
    database = ArchiveDatabase(tmp_path / "observations.sqlite3")
    database.initialize()
    database.upsert_auction("private-auction-id")
    database.upsert_vehicle("private-car-id")
    database.upsert_auction_lot("private-car-id", "private-auction-id")

    with sqlite3.connect(database.path) as connection:
        lot_id = connection.execute(
            """
            SELECT l.id
            FROM auction_lots AS l
            JOIN vehicles AS v ON v.id = l.vehicle_id
            WHERE v.source_car_id = ?
            """,
            ("private-car-id",),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO price_observations (
                auction_lot_id, highest_offer, currency, observed_at, source
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (lot_id, "12500.00", "EUR", "2026-11-25T08:00:00+00:00", "car_api"),
        )
        connection.execute(
            """
            INSERT INTO price_reference_observations (
                auction_lot_id, minimum_price, indicative_price, currency,
                observed_at, source
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                lot_id,
                "14000.00",
                "15000.00",
                "EUR",
                "2026-11-25T08:01:00+00:00",
                "car_api",
            ),
        )
        connection.execute(
            """
            INSERT INTO lot_price_observations (
                auction_lot_id, price_type, amount, currency, raw_text,
                source_field, observed_at, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lot_id,
                "fixed_price",
                "17500.00",
                "EUR",
                "Buy now for EUR 17,500",
                "fixed_price",
                "2026-11-25T08:02:00+00:00",
                "detail_html",
            ),
        )
        connection.execute(
            """
            INSERT INTO auction_events (
                auction_lot_id, auction_phase, bid_status, final_call_active,
                live_bidding, auction_end_at, remaining_seconds, observed_at, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lot_id,
                "LIVE",
                "BIDDING",
                0,
                1,
                "2026-11-25T09:00:00+00:00",
                3600,
                "2026-11-25T08:03:00+00:00",
                "car_api",
            ),
        )

    result = run_report(database.path)

    assert result.returncode == 0, result.stderr + result.stdout
    assert "highest_offer=12500.00 EUR" in result.stdout
    assert "not independently verified as a public bid" in result.stdout
    assert "minimum_price=14000.00; indicative_price=15000.00" in result.stdout
    assert "price_type=fixed_price; amount=17500.00 EUR" in result.stdout
    assert "source_field=fixed_price" in result.stdout
    assert "phase=LIVE; bid_status=BIDDING" in result.stdout
    assert "remaining_seconds=3600" in result.stdout
    assert "Auction-level scheduled start: not stored in schema v18" in result.stdout
    assert "private-auction-id" not in result.stdout
    assert "private-car-id" not in result.stdout
    assert "Buy now for EUR 17,500" not in result.stdout

