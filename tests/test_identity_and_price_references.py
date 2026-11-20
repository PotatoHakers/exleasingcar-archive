import sqlite3
from decimal import Decimal

from exleasingcar_archive.api_parser import parse_car_update_response
from exleasingcar_archive.database import ArchiveDatabase
from exleasingcar_archive.ingestion import AuctionContext, LotMetadata, store_car_updates
from exleasingcar_archive.metadata_parser import (
    parse_auction_catalog_metadata,
    parse_vehicle_metadata,
)


def test_catalog_extracts_make_and_mileage_without_guessing_model():
    html = """
    <div class="s-car-list auto_123" car-id="123" id="auto_123" code="x">
      <img class="card-img-top" alt="CITROEN">
      <h5>Citroen C3 - C3 BlueHDi 100 BVM6 You</h5>
      <div class="mb-2 px-2">22/02/2023</div>
      <div class="mb-2 px-2">Дизель</div>
      <div class="mb-2 px-2">1499см ³</div>
      <div class="mb-2 px-2">Механическая</div>
      <div class="mb-2 px-2">74,002km</div>
    </div>
    <input type="hidden" name="aukciono_id" value="210275">
    """

    catalog = parse_auction_catalog_metadata(html)
    lot = catalog.lots[0]

    assert catalog.exleasingcar_auction_id == "210275"
    assert lot.make_label == "CITROEN"
    assert lot.displayed_title == "Citroen C3 - C3 BlueHDi 100 BVM6 You"
    assert lot.mileage_km == 74002


def test_masked_chassis_number_is_saved_only_as_fragment():
    html = """
    <h1>Citroen C3 - C3 BlueHDi</h1>
    <div class="module-body auto-body" car-id="123" id="auto_body_123">
      <table>
        <tr><td>Номер кузова</td><td>VF7SXYHTUNT6<a>*****</a></td></tr>
      </table>
    </div>
    <div>ID аукциона: <a href="/ru/auction-210275">210275</a></div>
    """

    vehicle = parse_vehicle_metadata(html)

    assert vehicle.chassis_number is None
    assert vehicle.chassis_number_fragment == "VF7SXYHTUNT6"
    assert vehicle.chassis_number_masked is True


def test_real_response_zero_highest_offer_and_seller_minimum_are_separate(tmp_path):
    response = {
        "19655238": {
            "car_id": "19655238",
            "highest_offer": 0,
            "user_offer": 0,
            "text_minimali_kaina": 7500,
            "text_orientacine_kaina": 7500,
            "car_auction_time": {"laikas": "2026-11-13 09:00", "type": ""},
            "liko_s": 775,
        }
    }
    update = parse_car_update_response(response)["19655238"]

    assert update.highest_offer == Decimal(0)
    assert update.minimum_price == Decimal(7500)
    assert update.indicative_price == Decimal(7500)

    database = ArchiveDatabase(tmp_path / "fresh.sqlite3")
    database.initialize()
    summary = store_car_updates(
        database,
        {"19655238": update},
        AuctionContext("210275", source_auction_name="VPauto"),
        observed_at=None,
        lot_metadata={"19655238": LotMetadata(mileage_km=74002)},
    )

    assert summary.prices_inserted == 1
    assert summary.price_references_inserted == 1
    assert summary.events_inserted == 1
    with sqlite3.connect(database.path) as connection:
        bid_count = connection.execute(
            "SELECT COUNT(*) FROM price_observations"
        ).fetchone()[0]
        reference = connection.execute(
            "SELECT minimum_price, indicative_price "
            "FROM price_reference_observations"
        ).fetchone()
        mileage = connection.execute(
            "SELECT mileage_km FROM auction_lots"
        ).fetchone()[0]
        event = connection.execute(
            "SELECT auction_end_at, remaining_seconds FROM auction_events"
        ).fetchone()

    assert bid_count == 1
    with sqlite3.connect(database.path) as connection:
        highest_offer = connection.execute(
            "SELECT highest_offer FROM price_observations"
        ).fetchone()[0]
    assert highest_offer == "0"
    assert reference == ("7500", "7500")
    assert mileage == 74002
    assert event == ("2026-11-13 09:00", 775)


def test_complete_chassis_number_is_stored_normalized(tmp_path):
    database = ArchiveDatabase(tmp_path / "fresh.sqlite3")
    database.initialize()
    database.upsert_vehicle("car-1", chassis_number="vf7-sxyhtunt6abc123")

    with sqlite3.connect(database.path) as connection:
        value = connection.execute(
            "SELECT chassis_number FROM vehicles WHERE source_car_id = 'car-1'"
        ).fetchone()[0]

    assert value == "VF7SXYHTUNT6ABC123"


def test_relisted_vehicle_keeps_a_separate_lot_per_auction(tmp_path):
    database = ArchiveDatabase(tmp_path / "relisted.sqlite3")
    database.initialize()

    database.record_price_observation(
        "same-car-id",
        Decimal(9000),
        exleasingcar_auction_id="auction-first",
    )
    database.record_price_reference_observation(
        "same-car-id",
        minimum_price=Decimal(8500),
        exleasingcar_auction_id="auction-first",
    )
    database.record_price_observation(
        "same-car-id",
        Decimal(9200),
        exleasingcar_auction_id="auction-second",
    )
    database.record_price_reference_observation(
        "same-car-id",
        minimum_price=Decimal(8800),
        exleasingcar_auction_id="auction-second",
    )

    with sqlite3.connect(database.path) as connection:
        vehicle_count = connection.execute(
            "SELECT COUNT(*) FROM vehicles"
        ).fetchone()[0]
        lots = connection.execute(
            """
            SELECT a.exleasingcar_auction_id, p.highest_offer, r.minimum_price
            FROM auction_lots AS l
            JOIN auctions AS a ON a.id = l.auction_id
            JOIN price_observations AS p ON p.auction_lot_id = l.id
            JOIN price_reference_observations AS r ON r.auction_lot_id = l.id
            ORDER BY a.exleasingcar_auction_id
            """
        ).fetchall()

    assert vehicle_count == 1
    assert lots == [
        ("auction-first", "9000", "8500"),
        ("auction-second", "9200", "8800"),
    ]


def test_complete_chassis_number_is_extracted_from_detail_page():
    vehicle = parse_vehicle_metadata(
        """
        <div class="auto-body" car-id="123"></div>
        <table><tr><td>Номер кузова</td><td>vf7-sxyhtunt6abc123</td></tr></table>
        """
    )

    assert vehicle.chassis_number == "VF7SXYHTUNT6ABC123"
    assert vehicle.chassis_number_fragment is None
    assert vehicle.chassis_number_masked is False


def test_reimporting_same_seller_reference_price_does_not_duplicate_it(tmp_path):
    database = ArchiveDatabase(tmp_path / "dedup.sqlite3")
    database.initialize()
    updates = parse_car_update_response(
        {
            "car-1": {
                "highest_offer": 0,
                "text_minimali_kaina": 7500,
                "text_orientacine_kaina": 7500,
            }
        }
    )
    auction = AuctionContext("auction-1")

    first = store_car_updates(database, updates, auction)
    second = store_car_updates(database, updates, auction)

    assert first.price_references_inserted == 1
    assert second.price_references_inserted == 0
    with sqlite3.connect(database.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM price_reference_observations"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM price_observations"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT highest_offer FROM price_observations"
        ).fetchone()[0] == "0"
