from datetime import datetime, timezone

from exleasingcar_archive.database import ArchiveDatabase
from exleasingcar_archive.routine_runner import _completed_bootstrap_items


def test_routine_inventory_contains_only_completed_bootstrap_auctions(tmp_path):
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    observed_at = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)

    for auction_id in ("completed-auction", "unfinished-auction", "unlinked-auction"):
        database.upsert_auction(
            auction_id,
            source_url=(
                f"https://www.exleasingcar.com/auction-{auction_id}/show-20/"
                if auction_id != "unlinked-auction"
                else None
            ),
        )
        if auction_id != "unlinked-auction":
            database.enqueue_bootstrap_auction(auction_id, observed_at=observed_at)

    completed_scan = database.start_or_resume_catalog_scan(
        "completed-auction",
        "bootstrap",
        "https://www.exleasingcar.com/auction-completed-auction/show-20/",
        observed_at=observed_at,
    )
    database.complete_catalog_scan(completed_scan.scan_id, observed_at=observed_at)
    database.mark_bootstrap_auction_started(
        "completed-auction", observed_at=observed_at
    )
    database.mark_bootstrap_auction_completed(
        "completed-auction", observed_at=observed_at
    )

    items = _completed_bootstrap_items(database)

    assert [item.auction_id for item in items] == ["completed-auction"]
    assert items[0].status == "completed"
    assert items[0].source_url.endswith("/show-20/")
