from pathlib import Path

from exleasingcar_archive.metadata_parser import (
    parse_auction_catalog_metadata,
    parse_auction_listings,
    parse_vehicle_metadata,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_auction_listing_with_source_name_country_and_status():
    result = parse_auction_listings(
        _read_fixture("auction_listings.html"),
        "https://www.exleasingcar.com/ru/авто-аукционы",
    )

    assert len(result) == 1
    auction = result[0]
    assert auction.exleasingcar_auction_id == "210547"
    assert auction.source_auction_name == "BCA DE 3.3"
    assert auction.source_auction_country_code == "DE"
    assert auction.source_auction_country_name == "Германия"
    assert auction.auction_type_label == "Английский аукцион"
    assert auction.auction_status_label == "FINAL CALL"
    assert auction.vehicle_count == 76
    assert auction.source_url is not None
    assert "аукцион-210547" in auction.source_url


def test_parse_auction_catalog_lot_identifiers():
    result = parse_auction_catalog_metadata(
        _read_fixture("auction_catalog.html"),
        "https://www.exleasingcar.com/ru/авто-аукционы/аукцион-210547",
    )

    assert result.exleasingcar_auction_id == "210547"
    assert len(result.lots) == 1
    lot = result.lots[0]
    assert lot.source_car_id == "19702473"
    assert lot.displayed_lot_reference == "2258673/41"
    assert lot.listing_code_attribute == "2352500179"


def test_parse_vehicle_detail_metadata_and_keep_countries_separate():
    result = parse_vehicle_metadata(
        _read_fixture("vehicle_detail.html"),
        "https://www.exleasingcar.com/ru/auto-details/19702473",
    )

    assert result.source_car_id == "19702473"
    assert result.exleasingcar_auction_id == "210547"
    assert result.title == (
        "Mercedes-Benz E-KLASSE - 220 d 4Matic T 9G-TRONIC"
    )
    assert result.location_text == "DE-04509, Германия"
    assert result.location_country_code == "DE"
    assert result.location_country_name == "Германия"
    assert result.last_registration_country_code == "DE"
    assert result.last_registration_country_name == "Германия"


def test_missing_country_fields_remain_unknown():
    result = parse_vehicle_metadata(
        """
        <h1>Example vehicle</h1>
        <span>ID аукциона: <a href='/auction-123'>123</a></span>
        <table><tr><td>Место расположения:</td><td>Unknown</td></tr></table>
        """,
        "https://www.exleasingcar.com/ru/auto-details/98765",
    )

    assert result.source_car_id == "98765"
    assert result.exleasingcar_auction_id == "123"
    assert result.location_text == "Unknown"
    assert result.location_country_code is None
    assert result.location_country_name is None
    assert result.last_registration_country_code is None
    assert result.last_registration_country_name is None


def test_auction_id_is_not_confused_with_vehicle_id():
    result = parse_vehicle_metadata(
        """
        <div>ID автомобиля: 19702473</div>
        <div>ID аукциона:
          <a href='/ru/auction-210547'>210547</a>
        </div>
        """,
    )

    assert result.source_car_id is None
    assert result.exleasingcar_auction_id == "210547"
