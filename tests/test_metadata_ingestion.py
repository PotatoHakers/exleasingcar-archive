import sqlite3
from pathlib import Path

import pytest

from exleasingcar_archive.database import ArchiveDatabase
from exleasingcar_archive.metadata_ingestion import import_html_snapshots

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _import(database: ArchiveDatabase, **overrides):
    args = {
        "auction_list_html": _read("auction_listings.html"),
        "catalog_html": _read("auction_catalog.html"),
        "vehicle_detail_html": _read("vehicle_detail.html"),
    }
    args.update(overrides)
    return import_html_snapshots(database, **args)


def test_html_metadata_is_written_to_database(tmp_path):
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()

    summary = _import(database)

    assert summary.auctions_seen == 1
    assert summary.lots_seen == 1
    assert summary.vehicle_details_enriched == 1
    assert summary.selected_auction_id == "210547"
    assert summary.selected_car_id == "19702473"
    assert summary.api_ingestion is None

    with sqlite3.connect(database.path) as connection:
        auction = connection.execute(
            """
            SELECT exleasingcar_auction_id, source_auction_name,
                   source_auction_country_code, auction_type_label,
                   auction_status_label
            FROM auctions
            """
        ).fetchone()
        vehicle = connection.execute(
            """
            SELECT source_car_id, displayed_title, location_text,
                   location_country_code, last_registration_country_code
            FROM vehicles
            """
        ).fetchone()
        lot = connection.execute(
            """
            SELECT l.displayed_lot_reference, l.listing_code_attribute,
                   a.exleasingcar_auction_id
            FROM auction_lots AS l
            JOIN auctions AS a ON a.id = l.auction_id
            """
        ).fetchone()
        price_count = connection.execute(
            "SELECT COUNT(*) FROM price_observations"
        ).fetchone()[0]
        markers = connection.execute(
            """
            SELECT icon_filename, marker_group, label, is_present
            FROM auction_markers
            """
        ).fetchall()

    assert auction == (
        "210547", "BCA DE 3.3", "DE", "Английский аукцион", "FINAL CALL"
    )
    assert vehicle == (
        "19702473",
        "Mercedes-Benz E-KLASSE - 220 d 4Matic T 9G-TRONIC",
        "DE-04509, Германия",
        "DE",
        "DE",
    )
    assert lot == ("2258673/41", "2352500179", "210547")
    assert price_count == 0
    assert markers == [
        ("ac.svg", "auction_format", "Английский аукцион", 1)
    ]


def test_reimporting_same_html_does_not_duplicate_auctions_or_lots(tmp_path):
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()

    _import(database)
    _import(database)

    with sqlite3.connect(database.path) as connection:
        counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("auctions", "vehicles", "auction_lots")
        )

    assert counts == (1, 1, 1)


def test_detail_page_for_different_vehicle_is_rejected_before_writing(tmp_path):
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    wrong_detail = _read("vehicle_detail.html").replace("19702473", "99999999")

    with pytest.raises(ValueError, match="not present in the supplied auction catalogue"):
        _import(database, vehicle_detail_html=wrong_detail)

    with sqlite3.connect(database.path) as connection:
        auction_count = connection.execute("SELECT COUNT(*) FROM auctions").fetchone()[0]
        vehicle_count = connection.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]

    assert auction_count == 0
    assert vehicle_count == 0


def test_api_car_not_in_catalogue_is_rejected_before_writing(tmp_path):
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    api_response = '{"different-car": {"highest_offer": "18300"}}'

    with pytest.raises(ValueError, match="outside the supplied catalogue"):
        _import(database, api_response=api_response)

    with sqlite3.connect(database.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM auctions").fetchone()[0] == 0


def test_api_response_is_attached_to_the_matching_catalogue_lot(tmp_path):
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    # Synthetic API values used only to exercise the end-to-end ingestion path.
    api_response = {
        "19702473": {
            "highest_offer": "18300",
            "car_auction_time": {
                "type": "final-call",
                "image": "final-call-active",
                "liveBidding": "0",
                "remainingSeconds": 20,
            },
        }
    }

    summary = _import(database, api_response=api_response)

    assert summary.api_ingestion is not None
    assert summary.api_ingestion.prices_inserted == 1
    assert summary.api_ingestion.events_inserted == 1

    with sqlite3.connect(database.path) as connection:
        row = connection.execute(
            """
            SELECT a.exleasingcar_auction_id, v.source_car_id, p.highest_offer
            FROM price_observations AS p
            JOIN auction_lots AS l ON l.id = p.auction_lot_id
            JOIN auctions AS a ON a.id = l.auction_id
            JOIN vehicles AS v ON v.id = l.vehicle_id
            """
        ).fetchone()

    assert row == ("210547", "19702473", "18300")


def test_fixed_price_detail_is_imported_into_separate_price_history(tmp_path):
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    fixed_detail = _read("vehicle_detail.html").replace(
        "</body>",
        """
        <div class="auto-block" car-id="19702473">
            <div class="bids active fix bids_19702473"></div>
            <div class="fixed-price">
                <input class="buy-offer" value="Купить сейчас за € 16 900">
            </div>
        </div>
        </body>
        """,
    )

    summary = _import(database, vehicle_detail_html=fixed_detail)

    assert summary.sale_type_recognized is True
    assert summary.lot_price_observations_inserted == 1
    with sqlite3.connect(database.path) as connection:
        lot = connection.execute("SELECT sale_type FROM auction_lots").fetchone()[0]
        fixed = connection.execute(
            "SELECT price_type, amount, currency FROM lot_price_observations"
        ).fetchone()
        bid_count = connection.execute(
            "SELECT COUNT(*) FROM price_observations"
        ).fetchone()[0]

    assert lot == "fixed_price"
    assert fixed == ("fixed_price", "16900", "EUR")
    assert bid_count == 0
