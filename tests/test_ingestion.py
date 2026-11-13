import sqlite3
from datetime import datetime, timezone

import pytest

from exleasingcar_archive.api_parser import parse_car_update_response
from exleasingcar_archive.database import ArchiveDatabase
from exleasingcar_archive.ingestion import (
    AuctionContext,
    LotMetadata,
    store_car_updates,
)

OBSERVED_AT = datetime(2026, 11, 12, 13, 19, 30, tzinfo=timezone.utc)
AUCTION = AuctionContext(
    exleasingcar_auction_id="test-auction-001",
    source_auction_name="Test source auction",
    source_auction_country_code="DE",
    source_auction_country_name="Germany",
    auction_type_label="English auction",
    auction_status_label="FINAL CALL",
)
LOT_METADATA = {
    "test-car-001": LotMetadata(
        displayed_lot_reference="TEST-LOT-001",
        listing_code_attribute="TEST-CODE-001",
    )
}


def _active_final_call_response():
    # Synthetic values for the integration test; this is not a captured
    # response for a real vehicle or a claim about a real auction result.
    return parse_car_update_response(
        {
            "test-car-001": {
                "user_offer": 18400,
                "highest_offer": "18300",
                "actual_user_offer": 0,
                "actual_car_price": "0",
                "car_id": "test-car-001",
                "aukciono_tipas": "1",
                "car_auction_time": {
                    "image": "final-call-active",
                    "type": "final-call",
                    "liveBidding": "0",
                    "endDate": "2026-11-12 13:19:57",
                    "remainingSeconds": 20,
                },
            }
        }
    )


def test_store_api_update_with_auction_and_lot_context(tmp_path):
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()

    summary = store_car_updates(
        database,
        _active_final_call_response(),
        AUCTION,
        observed_at=OBSERVED_AT,
        lot_metadata=LOT_METADATA,
    )

    assert summary.cars_processed == 1
    assert summary.lots_upserted == 1
    assert summary.prices_inserted == 1
    assert summary.events_inserted == 1

    with sqlite3.connect(database.path) as connection:
        auction = connection.execute(
            """
            SELECT exleasingcar_auction_id, source_auction_name,
                   source_auction_country_code, source_auction_country_name
            FROM auctions
            """
        ).fetchone()
        lot = connection.execute(
            """
            SELECT v.source_car_id, a.exleasingcar_auction_id,
                   l.displayed_lot_reference, l.listing_code_attribute
            FROM auction_lots AS l
            JOIN vehicles AS v ON v.id = l.vehicle_id
            JOIN auctions AS a ON a.id = l.auction_id
            """
        ).fetchone()
        price = connection.execute(
            "SELECT highest_offer FROM price_observations"
        ).fetchone()
        event = connection.execute(
            """
            SELECT auction_phase, final_call_active, live_bidding,
                   remaining_seconds
            FROM auction_events
            """
        ).fetchone()

    assert auction == ("test-auction-001", "Test source auction", "DE", "Germany")
    assert lot == ("test-car-001", "test-auction-001", "TEST-LOT-001", "TEST-CODE-001")
    assert price == ("18300",)
    assert event == ("final-call", 1, 0, 20)


def test_repeated_unchanged_response_does_not_duplicate_history(tmp_path):
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    updates = _active_final_call_response()

    first = store_car_updates(
        database, updates, AUCTION, observed_at=OBSERVED_AT
    )
    second = store_car_updates(
        database,
        updates,
        AUCTION,
        observed_at=datetime(2026, 11, 12, 13, 19, 35, tzinfo=timezone.utc),
    )

    assert first.prices_inserted == 1
    assert first.events_inserted == 1
    assert second.prices_inserted == 0
    assert second.events_inserted == 0

    with sqlite3.connect(database.path) as connection:
        price_count = connection.execute(
            "SELECT COUNT(*) FROM price_observations"
        ).fetchone()[0]
        event_count = connection.execute(
            "SELECT COUNT(*) FROM auction_events"
        ).fetchone()[0]

    assert price_count == 1
    assert event_count == 1


def test_missing_price_still_registers_lot_and_event(tmp_path):
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    updates = parse_car_update_response(
        {
            "test-car-001": {
                "car_auction_time": {
                    "type": "final-call",
                    "image": "final-call-active",
                }
            }
        }
    )

    summary = store_car_updates(
        database, updates, AUCTION, observed_at=OBSERVED_AT
    )

    assert summary.lots_upserted == 1
    assert summary.prices_inserted == 0
    assert summary.events_inserted == 1

    with sqlite3.connect(database.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM auction_lots"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM price_observations"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM auction_events"
        ).fetchone()[0] == 1


def test_missing_timer_does_not_create_false_only_event(tmp_path):
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    updates = parse_car_update_response({"test-car-001": {"highest_offer": "18300"}})

    summary = store_car_updates(
        database, updates, AUCTION, observed_at=OBSERVED_AT
    )

    assert summary.prices_inserted == 1
    assert summary.events_inserted == 0

    with sqlite3.connect(database.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM auction_events"
        ).fetchone()[0] == 0


def test_update_key_must_match_car_id(tmp_path):
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    updates = _active_final_call_response()

    with pytest.raises(ValueError, match="does not match"):
        store_car_updates(
            database,
            {"wrong-id": updates["test-car-001"]},
            AUCTION,
            observed_at=OBSERVED_AT,
        )

    with sqlite3.connect(database.path) as connection:
        auction_count = connection.execute(
            "SELECT COUNT(*) FROM auctions"
        ).fetchone()[0]

    assert auction_count == 0
