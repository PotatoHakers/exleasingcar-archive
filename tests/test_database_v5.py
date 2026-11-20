import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from exleasingcar_archive.database import (
    AUCTION_EVENTS_SQL,
    AUCTION_LOTS_SQL,
    AUCTIONS_SQL,
    PRICE_OBSERVATIONS_SQL,
    PRICE_REFERENCE_OBSERVATIONS_SQL,
    SCHEMA_VERSION,
    VEHICLES_SQL,
    ArchiveDatabase,
)
from exleasingcar_archive.models import PriceType, SaleType
from exleasingcar_archive.parsers import PriceObservation

OBSERVED = datetime(2026, 11, 16, 16, 0, tzinfo=timezone.utc)


def _price(price_type, amount, source_field):
    return PriceObservation(
        price_type=price_type,
        amount=Decimal(amount),
        currency="EUR",
        raw_text=f"€ {amount}",
        source_field=source_field,
    )


def test_sale_type_and_non_bid_prices_are_stored_separately_and_deduplicated(tmp_path):
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()

    database.upsert_auction_lot(
        "car-1", "auction-1", sale_type=SaleType.FIXED_PRICE, observed_at=OBSERVED
    )
    fixed = _price(PriceType.FIXED_PRICE, "16900", "fixed-price .buy-offer")
    assert database.record_lot_price_observation(
        "car-1", fixed, OBSERVED, exleasingcar_auction_id="auction-1",
        sale_type=SaleType.FIXED_PRICE,
    )
    assert not database.record_lot_price_observation(
        "car-1", fixed, OBSERVED, exleasingcar_auction_id="auction-1",
        sale_type=SaleType.FIXED_PRICE,
    )

    changed = _price(PriceType.FIXED_PRICE, "16500", "fixed-price .buy-offer")
    assert database.record_lot_price_observation(
        "car-1", changed, OBSERVED, exleasingcar_auction_id="auction-1",
        sale_type=SaleType.FIXED_PRICE,
    )

    # A public fixed price must never appear in the bid-history table.
    with sqlite3.connect(database.path) as connection:
        lot = connection.execute(
            """
            SELECT l.sale_type FROM auction_lots l
            JOIN vehicles v ON v.id = l.vehicle_id
            WHERE v.source_car_id = 'car-1'
            """
        ).fetchone()
        price_rows = connection.execute(
            "SELECT price_type, amount, currency FROM lot_price_observations ORDER BY id"
        ).fetchall()
        bid_count = connection.execute(
            "SELECT COUNT(*) FROM price_observations"
        ).fetchone()[0]

    assert lot == ("fixed_price",)
    assert price_rows == [
        ("fixed_price", "16900", "EUR"),
        ("fixed_price", "16500", "EUR"),
    ]
    assert bid_count == 0


def test_unknown_sale_type_does_not_erase_previously_known_type(tmp_path):
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    database.upsert_auction_lot("car-1", "auction-1", sale_type=SaleType.OPEN_AUCTION)
    database.upsert_auction_lot("car-1", "auction-1", sale_type=None)

    with sqlite3.connect(database.path) as connection:
        sale_type = connection.execute(
            "SELECT sale_type FROM auction_lots"
        ).fetchone()[0]
    assert sale_type == "open_auction"


def test_relisted_vehicle_keeps_sale_type_and_price_history_per_auction(tmp_path):
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    database.record_lot_price_observation(
        "same-car", _price(PriceType.FIXED_PRICE, "16900", "buy-now"),
        exleasingcar_auction_id="auction-first", sale_type=SaleType.FIXED_PRICE,
    )
    database.record_lot_price_observation(
        "same-car", _price(PriceType.DISPLAYED_AUCTION_PRICE, "8000", "current-price"),
        exleasingcar_auction_id="auction-second", sale_type=SaleType.OPEN_AUCTION,
    )

    with sqlite3.connect(database.path) as connection:
        rows = connection.execute(
            """
            SELECT a.exleasingcar_auction_id, l.sale_type,
                   p.price_type, p.amount
            FROM lot_price_observations p
            JOIN auction_lots l ON l.id = p.auction_lot_id
            JOIN auctions a ON a.id = l.auction_id
            ORDER BY a.exleasingcar_auction_id
            """
        ).fetchall()
        vehicle_count = connection.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]

    assert rows == [
        ("auction-first", "fixed_price", "fixed_price", "16900"),
        ("auction-second", "open_auction", "displayed_auction_price", "8000"),
    ]
    assert vehicle_count == 1


def test_v4_migration_adds_sale_type_and_price_history_without_losing_existing_rows(tmp_path):
    path = tmp_path / "archive-v4.sqlite3"
    old_lots_sql = AUCTION_LOTS_SQL.replace("    sale_type TEXT,\n", "")
    with sqlite3.connect(path) as connection:
        for statement in (
            AUCTIONS_SQL,
            VEHICLES_SQL,
            old_lots_sql,
            PRICE_OBSERVATIONS_SQL,
            AUCTION_EVENTS_SQL,
            PRICE_REFERENCE_OBSERVATIONS_SQL,
        ):
            connection.execute(statement)
        connection.execute(
            "INSERT INTO auctions (exleasingcar_auction_id, first_seen_at, last_seen_at) "
            "VALUES ('auction-old', '2026-11-15T00:00:00Z', '2026-11-15T00:00:00Z')"
        )
        connection.execute(
            "INSERT INTO vehicles (source_car_id, first_seen_at, last_seen_at) "
            "VALUES ('car-old', '2026-11-15T00:00:00Z', '2026-11-15T00:00:00Z')"
        )
        vehicle_id = connection.execute("SELECT id FROM vehicles").fetchone()[0]
        auction_id = connection.execute("SELECT id FROM auctions").fetchone()[0]
        connection.execute(
            "INSERT INTO auction_lots (vehicle_id, auction_id, first_seen_at, last_seen_at) "
            "VALUES (?, ?, '2026-11-15T00:00:00Z', '2026-11-15T00:00:00Z')",
            (vehicle_id, auction_id),
        )
        lot_id = connection.execute("SELECT id FROM auction_lots").fetchone()[0]
        connection.execute(
            "INSERT INTO price_observations "
            "(auction_lot_id, highest_offer, observed_at, source) "
            "VALUES (?, '9000', '2026-11-15T00:00:00Z', 'test')",
            (lot_id,),
        )
        connection.execute("PRAGMA user_version = 4")

    database = ArchiveDatabase(path)
    database.initialize()

    with sqlite3.connect(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        existing_bid = connection.execute(
            "SELECT highest_offer FROM price_observations"
        ).fetchone()[0]
        sale_type = connection.execute(
            "SELECT sale_type FROM auction_lots"
        ).fetchone()[0]
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        fk_errors = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert version == SCHEMA_VERSION == 7
    assert existing_bid == "9000"
    assert sale_type is None
    assert "lot_price_observations" in tables
    assert fk_errors == []
