import subprocess
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from exleasingcar_archive.database import ArchiveDatabase

ROOT = Path(__file__).resolve().parents[1]
REPORT_SCRIPT = ROOT / "auction_coverage_report.py"


def test_auction_coverage_report_distinguishes_api_refresh_from_detail_visits(
    tmp_path,
):
    database = ArchiveDatabase(tmp_path / "coverage.sqlite3")
    database.initialize()
    database.upsert_auction("211225")
    database.upsert_vehicle("car-api-only", displayed_title="Audi A6 2.0 TDI")
    database.upsert_auction_lot(
        "car-api-only",
        "211225",
        sale_type="open_auction",
        collection_eligible=True,
    )
    database.upsert_vehicle("car-detail", displayed_title="BMW 320d")
    database.upsert_auction_lot(
        "car-detail",
        "211225",
        sale_type="open_auction",
        collection_eligible=True,
    )
    database.record_price_observation(
        "car-api-only",
        Decimal("10900"),
        observed_at=datetime.fromisoformat("2026-11-25T10:55:30+00:00"),
        exleasingcar_auction_id="211225",
    )
    with database._connect() as connection:
        connection.execute(
            """
            UPDATE auction_lots
            SET last_api_checked_at = ?,
                last_detail_attempted_at = ?,
                last_detail_checked_at = ?
            WHERE vehicle_id = (
                SELECT id FROM vehicles WHERE source_car_id = ?
            )
            """,
            (
                "2026-11-25T10:55:30+00:00",
                None,
                None,
                "car-api-only",
            ),
        )
        connection.execute(
            """
            UPDATE auction_lots
            SET last_api_checked_at = ?,
                last_detail_attempted_at = ?,
                last_detail_checked_at = ?
            WHERE vehicle_id = (
                SELECT id FROM vehicles WHERE source_car_id = ?
            )
            """,
            (
                "2026-11-25T09:00:00+00:00",
                "2026-11-25T10:55:00+00:00",
                "2026-11-25T10:55:01+00:00",
                "car-detail",
            ),
        )

    result = subprocess.run(
        [
            sys.executable,
            str(REPORT_SCRIPT),
            str(database.path),
            "211225",
            "--since",
            "2026-11-25T13:51:04+03:00",
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "Auction 211225: 2 archived lot(s)" in result.stdout
    assert "API checked during run: 1" in result.stdout
    assert "Detail pages attempted/checked during run: 1" in result.stdout
    assert "car-api-only | Audi A6 2.0 TDI" in result.stdout
    assert "10900 EUR" in result.stdout
    assert "car-detail | BMW 320d" in result.stdout
    assert "API checked can be YES even when highest_offer is unchanged" in result.stdout
