import sqlite3
from decimal import Decimal

from exleasingcar_archive.dashboard import DashboardHandler
from exleasingcar_archive.database import ArchiveDatabase
from exleasingcar_archive.models import PriceType, SaleType
from exleasingcar_archive.parsers import PriceObservation


def test_dashboard_exposes_sale_type_and_fixed_price_without_bid_mixing(tmp_path):
    database = ArchiveDatabase(tmp_path / "dashboard.sqlite3")
    database.initialize()
    database.upsert_vehicle("car-1", displayed_title="Audi A6")
    database.upsert_auction_lot("car-1", "auction-1", sale_type=SaleType.FIXED_PRICE)
    database.record_lot_price_observation(
        "car-1",
        PriceObservation(
            price_type=PriceType.FIXED_PRICE,
            amount=Decimal(16900),
            currency="EUR",
            raw_text="Купить сейчас за € 16 900",
            source_field="fixed-price .buy-offer",
        ),
        exleasingcar_auction_id="auction-1",
        sale_type=SaleType.FIXED_PRICE,
    )

    handler = object.__new__(DashboardHandler)
    handler.database_path = database.path
    payload = handler._load_data()

    assert payload["schema_version"] == 5
    assert payload["counts"]["lot_price_observations"] == 1
    assert payload["lots"][0]["sale_type"] == "fixed_price"
    assert payload["lot_prices"][0]["price_type"] == "fixed_price"
    assert payload["lot_prices"][0]["amount"] == "16900"
    assert payload["bids"] == []



def test_dashboard_search_matches_car_name_id_and_chassis_number(tmp_path):
    database = ArchiveDatabase(tmp_path / "dashboard.sqlite3")
    database.initialize()
    database.upsert_vehicle(
        "car-search",
        displayed_title="Mercedes-Benz E-Klasse",
        chassis_number="WDB12345678901234",
    )
    database.upsert_auction_lot("car-search", "auction-1")

    handler = object.__new__(DashboardHandler)
    handler.database_path = database.path

    for query in ("mercedes", "CAR-SEARCH", "WDB123"):
        payload = handler._load_data(search_query=query)
        assert [lot["car_id"] for lot in payload["lots"]] == ["car-search"]


def test_dashboard_search_finds_cars_outside_the_recent_500_rows(tmp_path):
    database = ArchiveDatabase(tmp_path / "dashboard.sqlite3")
    database.initialize()
    database.upsert_vehicle("car-0000", displayed_title="Old target vehicle")
    database.upsert_auction_lot("car-0000", "auction-1")

    with sqlite3.connect(database.path) as connection:
        auction_id = connection.execute(
            "SELECT id FROM auctions WHERE exleasingcar_auction_id = 'auction-1'"
        ).fetchone()[0]
        connection.execute(
            """
            UPDATE auction_lots
            SET last_seen_at = '2000-01-01T00:00:00Z'
            WHERE vehicle_id = (
                SELECT id FROM vehicles WHERE source_car_id = 'car-0000'
            )
            """
        )
        for index in range(1, 501):
            car_id = f"car-{index:04d}"
            cursor = connection.execute(
                """
                INSERT INTO vehicles (source_car_id, first_seen_at, last_seen_at)
                VALUES (?, '2099-01-01T00:00:00Z', '2099-01-01T00:00:00Z')
                """,
                (car_id,),
            )
            connection.execute(
                """
                INSERT INTO auction_lots (
                    vehicle_id, auction_id, first_seen_at, last_seen_at
                ) VALUES (?, ?, '2099-01-01T00:00:00Z', '2099-01-01T00:00:00Z')
                """,
                (cursor.lastrowid, auction_id),
            )

    handler = object.__new__(DashboardHandler)
    handler.database_path = database.path

    recent = handler._load_data()
    found = handler._load_data(search_query="car-0000")

    assert len(recent["lots"]) == 500
    assert "car-0000" not in {lot["car_id"] for lot in recent["lots"]}
    assert [lot["car_id"] for lot in found["lots"]] == ["car-0000"]
