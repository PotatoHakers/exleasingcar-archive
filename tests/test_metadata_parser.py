import sqlite3
from pathlib import Path
from urllib.parse import unquote

from exleasingcar_archive.database import ArchiveDatabase
from exleasingcar_archive.metadata_parser import (
    parse_auction_catalog_metadata,
    parse_auction_listings,
    parse_vehicle_metadata,
    parse_vehicle_media,
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
    assert lot.displayed_title == "Mercedes-Benz E-KLASSE 220 d 4Matic T 9G-TRONIC"
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



def test_parse_varied_auction_cards_with_encoded_links_and_company_names():
    result = parse_auction_listings(
        _read_fixture("auction_listings_varied.html"),
        "https://www.exleasingcar.com/ru/auto-auctions",
    )
    auctions = {auction.exleasingcar_auction_id: auction for auction in result}

    assert set(auctions) == {
        "210836",
        "210951",
        "210895",
        "210847",
        "210839",
        "210945",
        "210854",
        "210981",
        "210884",
    }

    expected = {
        "210836": ("VWE Remarketing", "FR", "Франция", "Закрытые торги аукциона", None),
        "210951": ("CarNext", "DE", "Германия", "Английский аукцион", "FINAL CALL"),
        "210895": ("BCA", "NL", "Нидерланды", "Английский аукцион", "FINAL CALL"),
        "210847": ("Alphabet", "DE", "Германия", "Английский аукцион", "FINAL CALL"),
        "210839": ("BCA SE", "SE", "Швеция", "Английский аукцион", "FINAL CALL"),
        "210945": ("eCarsTrade ARVAL", "BE", "Бельгия", "Английский аукцион", "HOT BID"),
        "210854": ("Copart Germany", "DE", "Германия", "Английский аукцион", "FINAL CALL"),
        "210981": ("eCarsTrade ALD", "BE", "Бельгия", "Закрытые торги аукциона", None),
    }

    for auction_id, values in expected.items():
        auction = auctions[auction_id]
        (
            expected_name,
            expected_country_code,
            expected_country_name,
            expected_type,
            expected_status,
        ) = values
        assert auction.source_auction_name == expected_name
        assert auction.source_auction_country_code == expected_country_code
        assert auction.source_auction_country_name == expected_country_name
        assert auction.auction_type_label == expected_type
        assert auction.auction_status_label == expected_status
        assert auction.source_url is not None
        assert f"аукцион-{auction_id}" in unquote(auction.source_url)

    # These two registration-only cards have an auction ID but no catalogue
    # address or company name. Do not invent either value.
    restricted = auctions["210884"]
    assert restricted.source_auction_name is None
    assert restricted.source_url is None



def test_parse_fixed_price_only_auction_without_overriding_mixed_auction_types():
    result = parse_auction_listings(
        _read_fixture("auction_listings_fixed_price.html"),
        "https://www.exleasingcar.com/ru/авто-аукционы",
    )
    auctions = {auction.exleasingcar_auction_id: auction for auction in result}

    fixed_only = auctions["209633"]
    assert fixed_only.source_auction_name == "Our Stock"
    assert fixed_only.source_auction_country_code == "LT"
    assert fixed_only.source_auction_country_name == "Литва"
    assert fixed_only.auction_type_label == "Фиксированная цена автомобилей"

    open_with_fixed_price_cars = auctions["210854"]
    assert open_with_fixed_price_cars.auction_type_label == "Английский аукцион"

    closed_with_fixed_price_cars = auctions["209954"]
    assert closed_with_fixed_price_cars.auction_type_label == "Закрытые торги аукциона"

    unknown = auctions["210999"]
    assert unknown.auction_type_label is None



def test_parse_all_independent_auction_card_markers_and_keep_primary_type():
    result = parse_auction_listings(
        _read_fixture("auction_listings_all_markers.html"),
        "https://www.exleasingcar.com/ru/авто-аукционы",
    )
    auctions = {auction.exleasingcar_auction_id: auction for auction in result}

    our_stock = auctions["209633"]
    our_stock_markers = {marker.icon_filename: marker for marker in our_stock.markers}
    assert set(our_stock_markers) == {"euro-sign.svg", "favorite.svg"}
    assert our_stock_markers["euro-sign.svg"].marker_group == "fixed_price_option"
    assert our_stock_markers["euro-sign.svg"].label == "Фиксированная цена автомобилей"
    assert our_stock_markers["favorite.svg"].marker_group == "recommendation"
    assert "тщательно проверен и рекомендован" in our_stock_markers["favorite.svg"].label
    assert our_stock.auction_type_label == "Фиксированная цена автомобилей"

    carnext = auctions["211030"]
    assert carnext.auction_type_label == "Английский аукцион"
    assert [marker.icon_filename for marker in carnext.markers] == ["ac.svg"]

    copart = auctions["210944"]
    copart_markers = {marker.icon_filename: marker for marker in copart.markers}
    assert set(copart_markers) == {
        "ac.svg",
        "euro-sign.svg",
        "type-2.svg",
    }
    assert copart.auction_type_label == "Английский аукцион"
    assert copart_markers["type-2.svg"].marker_group == "vehicle_condition"
    assert copart_markers["type-2.svg"].label == "Автомобиль с техническими неисправностями"

    vpauto = auctions["210630"]
    assert vpauto.auction_type_label == "Закрытые торги аукциона"
    assert [marker.icon_filename for marker in vpauto.markers] == ["mail-b.svg"]

    ald = auctions["211004"]
    assert ald.auction_type_label == "Закрытые торги аукциона"
    assert {marker.icon_filename for marker in ald.markers} == {
        "mail-b.svg",
        "euro-sign.svg",
    }



def test_catalog_make_uses_only_the_prefix_shared_with_the_displayed_title():
    saved_page = parse_auction_catalog_metadata(
        _read_fixture("auction_catalog_make_alt_mismatch.html"),
    )
    saved_lot = saved_page.lots[0]

    assert saved_lot.source_car_id == "19740683"
    assert saved_lot.displayed_title.startswith("Seat Leon")
    # The saved card's image alt says "SEAT XC40", while its title says
    # "Seat Leon". Keep the confirmed manufacturer, not the stale model.
    assert saved_lot.make_label == "SEAT"

    # Regression values reported for vehicle 19764527 in the dashboard:
    # the screenshot showed "FORD Movano" as Make and "Ford Focus..." as title.
    ford_page = parse_auction_catalog_metadata(
        """
        <input type="hidden" name="aukciono_id" value="210854">
        <div class="s-car-list auto-block" car-id="19764527">
          <img class="card-img-top" alt="FORD Movano">
          <h5>Ford Focus - 2021 Ford Focus Lim. ST-Line</h5>
        </div>
        """
    )
    assert ford_page.lots[0].make_label == "FORD"

    # Do not infer a make when the leading words of the independent fields
    # disagree.
    unrelated_alt_page = parse_auction_catalog_metadata(
        """
        <input type="hidden" name="aukciono_id" value="210854">
        <div class="s-car-list auto-block" car-id="car-unknown">
          <img class="card-img-top" alt="VOLKSWAGEN Golf">
          <h5>Ford Focus - 2021 Ford Focus Lim. ST-Line</h5>
        </div>
        """
    )
    assert unrelated_alt_page.lots[0].make_label is None



def test_real_masked_chassis_markup_is_parsed_and_persisted(tmp_path):
    html = """
    <html>
      <head>
        <link rel="canonical"
              href="https://www.exleasingcar.com/ru/auto-details/19810183">
      </head>
      <body>
        <div class="module-body auto-body" car-id="19810183"
             id="auto_body_19810183">
          <h1>AUDI A4 Av. 35TFSI S Ed. Comp. Aut 2021</h1>
          <table>
            <tbody>
              <tr class="border-bottom">
                <td class="border-0">Номер кузова</td>
                <td class="border-0">
                  WAUZZZF48NA0
                  <a href="/ru/завершение-регистрации/">*****</a>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </body>
    </html>
    """

    parsed = parse_vehicle_metadata(
        html,
        "https://www.exleasingcar.com/ru/auto-details/19810183",
    )

    assert parsed.source_car_id == "19810183"
    assert parsed.chassis_number is None
    assert parsed.chassis_number_fragment == "WAUZZZF48NA0"
    assert parsed.chassis_number_masked is True

    database = ArchiveDatabase(tmp_path / "masked-vin.sqlite3")
    database.initialize()
    database.upsert_vehicle(
        parsed.source_car_id,
        chassis_number=parsed.chassis_number,
        chassis_number_fragment=parsed.chassis_number_fragment,
        chassis_number_masked=parsed.chassis_number_masked,
    )

    # A later catalogue-only update must not erase the detail-page fragment.
    database.upsert_vehicle(parsed.source_car_id, displayed_title="Audi A4")

    with sqlite3.connect(database.path) as connection:
        stored = connection.execute(
            """
            SELECT chassis_number, chassis_number_fragment, chassis_number_masked
            FROM vehicles
            WHERE source_car_id = ?
            """,
            (parsed.source_car_id,),
        ).fetchone()

    assert stored == (None, "WAUZZZF48NA0", 1)



def test_parse_vehicle_media_extracts_gallery_originals_and_unique_reports():
    html = """
    <html><body>
      <div id="gallery-area">
        <div class="highslide-gallery">
          <a class="highslide" href="./saved_page_files/local-normal.jpg">
            <img src="./saved_page_files/local-normal.jpg">
          </a>
        </div>
        <div class="gallery-thumbs">
          <div class="photo-page">
            <a title="https://data.exleasingcar.com//auction_images/auction-123/456/car-normal_a.jpg"
               onclick="change_thumb(this, 'https://data.exleasingcar.com//auction_images/auction-123/456/car-normal_a.jpg')">
              <span class="photo">
                <img src="https://data.exleasingcar.com//auction_images/auction-123/456/car-small_a.jpg">
              </span>
            </a>
            <a onclick="change_thumb(this, 'https://data.exleasingcar.com//auction_images/auction-123/456/car-normal_b.jpg')">
              <img src="https://data.exleasingcar.com//auction_images/auction-123/456/car-small_b.jpg">
            </a>
            <a onclick="change_thumb(this, 'https://evil.example/car-normal_c.jpg')">
              <img src="https://evil.example/car-small_c.jpg">
            </a>
            <a onclick="change_thumb(this, 'http://data.exleasingcar.com/car-normal_d.jpg')">
              <img src="http://data.exleasingcar.com/car-small_d.jpg">
            </a>
          </div>
        </div>
      </div>
      <div class="damage-block">
        <table><tbody>
          <tr>
            <td onclick="window.open('https://data.exleasingcar.com/auction_files/123/456/report-a.pdf','Damages','width=775')">
              <strong>Inspection Report</strong>
            </td>
            <td onclick="window.open('https://data.exleasingcar.com/auction_files/123/456/report-a.pdf','Damages','width=775')">
              Review
            </td>
          </tr>
          <tr>
            <td onclick="window.open('https://data.exleasingcar.com/auction_files/123/456/report-b.pdf','Damages','width=775')">
              <strong>Inspection Report</strong>
            </td>
          </tr>
          <tr>
            <td onclick="window.open('https://evil.example/private.pdf','Damages')">
              <strong>Inspection Report</strong>
            </td>
          </tr>
        </tbody></table>
      </div>
    </body></html>
    """

    media = parse_vehicle_media(
        html,
        "https://www.exleasingcar.com/ru/auto-details/12345",
    )

    photos = [item for item in media if item.media_type == "photo"]
    reports = [item for item in media if item.media_type == "report"]

    assert len(photos) == 2
    assert len(reports) == 2
    assert photos[0].source_url.endswith("car-normal_a.jpg")
    assert photos[0].preview_url.endswith("car-small_a.jpg")
    assert photos[1].source_url.endswith("car-normal_b.jpg")
    assert reports[0].source_url.endswith("report-a.pdf")
    assert reports[0].label == "Inspection Report"
    assert reports[1].source_url.endswith("report-b.pdf")
    assert [item.source_order for item in media] == [0, 1, 2, 3]
