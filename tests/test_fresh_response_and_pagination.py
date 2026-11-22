import sqlite3
from decimal import Decimal
from pathlib import Path

from exleasingcar_archive.api_parser import parse_car_update_response
from exleasingcar_archive.database import ArchiveDatabase
from exleasingcar_archive.metadata_ingestion import import_html_snapshots
from exleasingcar_archive.metadata_parser import parse_catalog_pagination

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _open_detail() -> str:
    return _read("vehicle_detail.html").replace(
        "</body>",
        """
        <div class="auto-block" car-id="19702473">
            <div class="bids active open bids_19702473">
                <div class="current-price">Текущая цена: € 18 300</div>
            </div>
        </div>
        </body>
        """,
    )


def test_actual_response_fields_are_mapped_without_substituting_prices():
    response = {
        "19655238": {
            "car_id": "19655238",
            "highest_offer": 0,
            "user_offer": 0,
            "text_minimali_kaina": 7500,
            "text_orientacine_kaina": 7500,
            "car_auction_time": {
                "laikas": "2026-11-13 09:00",
                "hotbid_laikas": 1794553200,
                "type": "",
                "image": "",
            },
            "liko_s": 775,
        }
    }

    update = parse_car_update_response(response)["19655238"]

    assert update.highest_offer == Decimal(0)
    assert update.minimum_price == Decimal(7500)
    assert update.indicative_price == Decimal(7500)
    assert update.user_offer == Decimal(0)
    assert update.remaining_seconds == 775
    assert update.auction_end_at == "2026-11-13 09:00"
    assert update.auction_phase is None


def test_api_response_time_and_zero_price_are_written_to_fresh_sqlite(tmp_path):
    database = ArchiveDatabase(tmp_path / "fresh.sqlite3")
    database.initialize()
    api_response = {
        "19702473": {
            "car_id": "19702473",
            "highest_offer": 0,
            "text_minimali_kaina": 7500,
            "text_orientacine_kaina": 7500,
            "car_auction_time": {"laikas": "2026-11-13 09:00", "type": ""},
            "liko_s": 775,
        }
    }

    summary = import_html_snapshots(
        database,
        auction_list_html=_read("auction_listings.html"),
        catalog_html=_read("auction_catalog.html"),
        vehicle_detail_html=_open_detail(),
        api_response=api_response,
    )

    assert summary.api_ingestion is not None
    assert summary.api_ingestion.prices_inserted == 1
    assert summary.api_ingestion.price_references_inserted == 1
    assert summary.api_ingestion.events_inserted == 1
    with sqlite3.connect(database.path) as connection:
        price_count = connection.execute(
            "SELECT COUNT(*) FROM price_observations"
        ).fetchone()[0]
        references = connection.execute(
            "SELECT minimum_price, indicative_price "
            "FROM price_reference_observations"
        ).fetchone()
        event = connection.execute(
            "SELECT auction_end_at, remaining_seconds FROM auction_events"
        ).fetchone()
    assert price_count == 1
    with sqlite3.connect(database.path) as connection:
        highest_offer = connection.execute(
            "SELECT highest_offer FROM price_observations"
        ).fetchone()[0]
    assert highest_offer == "0"
    assert references == ("7500", "7500")
    assert event == ("2026-11-13 09:00", 775)


def test_catalog_pagination_discovers_second_page_from_saved_html():
    page_one_url = "https://www.exleasingcar.com/ru/auction-210275/show-20/"
    page_two_url = "https://www.exleasingcar.com/ru/auction-210275/show-20/2"
    html = "\n".join(
        [
            "<!doctype html>",
            "<!-- saved from url=(0069)https://www.exleasingcar.com/ru/auction-210275 -->",
            "<nav><ul class=\"pagination\">",
            f'<li><a class="pagination-page current page-link" href="{page_one_url}">1</a></li>',
            f'<li><a class="pagination-page page-link" href="{page_two_url}">2</a></li>',
            f'<li><a class="pagination-next page-link" href="{page_two_url}">next</a></li>',
            "</ul></nav>",
        ]
    )

    result = parse_catalog_pagination(html)

    assert result.current_page_number == 1
    assert result.current_page_url == (
        "https://www.exleasingcar.com/ru/auction-210275/show-20/"
    )
    assert result.next_page_url == (
        "https://www.exleasingcar.com/ru/auction-210275/show-20/2"
    )
    assert len(result.page_urls) == 2


def test_importer_merges_multiple_catalogue_pages(tmp_path):
    database = ArchiveDatabase(tmp_path / "multi-page.sqlite3")
    database.initialize()
    first_url = "https://www.exleasingcar.com/ru/auction-210547/show-20/"
    second_url = "https://www.exleasingcar.com/ru/auction-210547/show-20/2"
    page_one = _read("auction_catalog.html").replace(
        "</body>",
        f'''<nav><ul class="pagination">
          <li><a class="pagination-page current page-link" href="{first_url}">1</a></li>
          <li><a class="pagination-page page-link" href="{second_url}">2</a></li>
          <li><a class="pagination-next page-link" href="{second_url}">next</a></li>
        </ul></nav></body>''',
    )
    page_two = f'''<html><body>
      <input type="hidden" name="aukciono_id" value="210547">
      <div class="s-car-list auto-block" car-id="19702474" id="auto_19702474" code="987">
        <h5>Second page vehicle</h5><div class="mb-2 px-2">LOT-2</div>
      </div>
      <nav><ul class="pagination">
        <li><a class="pagination-page page-link" href="{first_url}">1</a></li>
        <li><a class="pagination-page current page-link" href="{second_url}">2</a></li>
      </ul></nav>
    </body></html>'''
    detail_two = _read("vehicle_detail.html").replace("19702473", "19702474")

    summary = import_html_snapshots(
        database,
        auction_list_html=_read("auction_listings.html"),
        catalog_html=page_one,
        additional_catalog_htmls=(page_two,),
        vehicle_detail_html=detail_two,
    )

    assert summary.catalog_pages_imported == 2
    assert summary.missing_catalog_page_urls == ()
    assert summary.lots_seen == 2
    with sqlite3.connect(database.path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM auction_lots").fetchone()[0]
    assert count == 2


def test_importer_warns_when_a_linked_catalogue_page_is_missing(tmp_path):
    database = ArchiveDatabase(tmp_path / "partial.sqlite3")
    database.initialize()
    first_url = "https://www.exleasingcar.com/ru/auction-210547/show-20/"
    second_url = "https://www.exleasingcar.com/ru/auction-210547/show-20/2"
    page_one = _read("auction_catalog.html").replace(
        "</body>",
        f'''<nav><ul class="pagination">
          <li><a class="pagination-page current page-link" href="{first_url}">1</a></li>
          <li><a class="pagination-page page-link" href="{second_url}">2</a></li>
          <li><a class="pagination-next page-link" href="{second_url}">next</a></li>
        </ul></nav></body>''',
    )

    summary = import_html_snapshots(
        database,
        auction_list_html=_read("auction_listings.html"),
        catalog_html=page_one,
        vehicle_detail_html=_read("vehicle_detail.html"),
    )

    assert summary.missing_catalog_page_urls == (second_url,)


def test_fresh_auction_card_fields_are_extracted_from_the_observed_structure():
    from exleasingcar_archive.metadata_parser import parse_auction_listings

    html = '''<a href="https://www.exleasingcar.com/ru/авто-аукционы/аукцион-210275">
      <div class="ac-list-item">
        <div class="ac-head-items">
          <div class="ac-head-item"><div class="ac-inner"><img alt="icon">31</div></div>
          <div class="ac-head-item"><img alt="Закрытые торги аукциона"></div>
          <div class="ac-head-item"><span>Аукцион ID:&nbsp;210275</span></div>
          <div class="ac-head-item"><div>VPauto</div></div>
        </div>
        <div class="ac-list-left">
          <span class="flag-image flag-fr"></span><span class="ml-2">Франция</span>
        </div>
      </div>
    </a>'''

    auctions = parse_auction_listings(html)

    assert len(auctions) == 1
    assert auctions[0].exleasingcar_auction_id == "210275"
    assert auctions[0].source_auction_name == "VPauto"
    assert auctions[0].source_auction_country_code == "FR"
    assert auctions[0].source_auction_country_name == "Франция"
    assert auctions[0].vehicle_count == 31


def test_catalog_auction_id_can_be_read_from_saved_source_url():
    from exleasingcar_archive.metadata_parser import parse_auction_catalog_metadata

    html = """<!doctype html>
    <!-- saved from url=(0070)https://www.exleasingcar.com/ru/auction-210275 -->
    <div class="s-car-list" car-id="19655238" id="auto_19655238"></div>
    """

    catalog = parse_auction_catalog_metadata(html)

    assert catalog.exleasingcar_auction_id == "210275"
    assert catalog.lots[0].source_car_id == "19655238"
