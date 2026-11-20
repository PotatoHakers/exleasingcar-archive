import sqlite3
from decimal import Decimal

from exleasingcar_archive.dashboard import PAGE, DashboardHandler
from exleasingcar_archive.database import ArchiveDatabase
from exleasingcar_archive.models import AuctionCardMarker, PriceType, SaleType
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

    assert payload["schema_version"] == 8
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



def test_dashboard_search_shows_full_history_across_relisted_auctions(tmp_path):
    database = ArchiveDatabase(tmp_path / "dashboard.sqlite3")
    database.initialize()
    database.upsert_vehicle(
        "car-target",
        displayed_title="Audi A6",
        chassis_number_fragment="WAUZZZ",
        chassis_number_masked=True,
    )
    database.upsert_auction_lot("car-target", "auction-old")
    database.upsert_auction_lot("car-target", "auction-new")
    database.upsert_vehicle("car-other", displayed_title="BMW 5 Series")
    database.upsert_auction_lot("car-other", "auction-other")

    with sqlite3.connect(database.path) as connection:
        lots = dict(connection.execute(
            """
            SELECT a.exleasingcar_auction_id, al.id
            FROM auction_lots al
            JOIN auctions a ON a.id = al.auction_id
            """
        ).fetchall())

        for auction_id, amount, observed_at in (
            ("auction-old", "8000", "2021-01-01T00:00:00Z"),
            ("auction-new", "9500", "2024-01-01T00:00:00Z"),
            ("auction-other", "12000", "2025-01-01T00:00:00Z"),
        ):
            lot_id = lots[auction_id]
            connection.execute(
                """
                INSERT INTO price_observations
                    (auction_lot_id, highest_offer, observed_at, source)
                VALUES (?, ?, ?, 'test')
                """,
                (lot_id, amount, observed_at),
            )
            connection.execute(
                """
                INSERT INTO price_reference_observations
                    (auction_lot_id, minimum_price, indicative_price, observed_at, source)
                VALUES (?, ?, ?, ?, 'test')
                """,
                (lot_id, amount, amount, observed_at),
            )
            connection.execute(
                """
                INSERT INTO lot_price_observations
                    (auction_lot_id, price_type, amount, currency, raw_text,
                     source_field, observed_at, source)
                VALUES (?, 'displayed_auction_price', ?, 'EUR', ?, 'test',
                        ?, 'test')
                """,
                (lot_id, amount, f"€ {amount}", observed_at),
            )
            connection.execute(
                """
                INSERT INTO auction_events
                    (auction_lot_id, auction_phase, bid_status, final_call_active,
                     live_bidding, auction_end_at, remaining_seconds, observed_at, source)
                VALUES (?, 'final-call', NULL, 1, 0, NULL, 20, ?, 'test')
                """,
                (lot_id, observed_at),
            )

    handler = object.__new__(DashboardHandler)
    handler.database_path = database.path

    all_data = handler._load_data()
    searched = handler._load_data(search_query="WAUZZZ")

    assert len(all_data["bids"]) == 3
    assert len(searched["lots"]) == 2
    assert {lot["auction_id"] for lot in searched["lots"]} == {
        "auction-old",
        "auction-new",
    }
    for history_name in ("bids", "references", "lot_prices", "events"):
        history = searched[history_name]
        assert len(history) == 2
        assert {row["auction_id"] for row in history} == {
            "auction-old",
            "auction-new",
        }
        assert {row["car_id"] for row in history} == {"car-target"}



def test_dashboard_car_card_shows_vehicle_details_and_all_auction_history(tmp_path):
    database = ArchiveDatabase(tmp_path / "dashboard.sqlite3")
    database.initialize()
    database.upsert_vehicle(
        "car-card-1",
        make="Audi",
        model="A6",
        displayed_title="Audi A6 3.0 TDI",
        chassis_number="WAUZZZ12345678901",
        location_text="DE-04509, Germany",
        location_country_code="DE",
        location_country_name="Germany",
        last_registration_country_code="DE",
        last_registration_country_name="Germany",
    )
    database.upsert_auction_lot(
        "car-card-1", "auction-old", mileage_km=120000
    )
    database.upsert_auction_lot(
        "car-card-1", "auction-new", mileage_km=135000
    )
    database.upsert_vehicle(
        "car-card-other",
        displayed_title="BMW 530d",
    )
    database.upsert_auction_lot("car-card-other", "auction-other")

    with sqlite3.connect(database.path) as connection:
        lot_ids = dict(connection.execute(
            """
            SELECT a.exleasingcar_auction_id, al.id
            FROM auction_lots al
            JOIN auctions a ON a.id = al.auction_id
            """
        ).fetchall())
        for auction_id, amount in (
            ("auction-old", "9000"),
            ("auction-new", "10500"),
            ("auction-other", "15000"),
        ):
            lot_id = lot_ids[auction_id]
            connection.execute(
                """
                INSERT INTO price_observations
                    (auction_lot_id, highest_offer, observed_at, source)
                VALUES (?, ?, ?, 'test')
                """,
                (lot_id, amount, f"2026-01-0{len(amount)}T00:00:00Z"),
            )
            connection.execute(
                """
                INSERT INTO auction_events
                    (auction_lot_id, auction_phase, final_call_active,
                     observed_at, source)
                VALUES (?, 'final-call', 1, '2026-01-01T00:00:00Z', 'test')
                """,
                (lot_id,),
            )

    handler = object.__new__(DashboardHandler)
    handler.database_path = database.path
    payload = handler._load_data(selected_car_id="car-card-1")
    later_page = handler._load_data(selected_car_id="car-card-1", page=2)

    assert len(later_page["lots"]) == 2
    assert later_page["pagination"]["page"] == 1
    assert later_page["pagination"]["has_next"] is False

    vehicle = payload["selected_vehicle"]
    assert vehicle["source_car_id"] == "car-card-1"
    assert vehicle["displayed_title"] == "Audi A6 3.0 TDI"
    assert vehicle["make"] == "Audi"
    assert vehicle["model"] == "A6"
    assert vehicle["chassis_number"] == "WAUZZZ12345678901"
    assert vehicle["location_text"] == "DE-04509, Germany"
    assert vehicle["last_registration_country_name"] == "Germany"

    assert {lot["auction_id"] for lot in payload["lots"]} == {
        "auction-old",
        "auction-new",
    }
    assert {lot["mileage_km"] for lot in payload["lots"]} == {120000, 135000}
    assert len(payload["bids"]) == 2
    assert len(payload["events"]) == 2
    assert {row["car_id"] for row in payload["bids"]} == {"car-card-1"}
    assert {row["car_id"] for row in payload["events"]} == {"car-card-1"}



def test_dashboard_returns_current_markers_per_auction_without_crossing_relisted_lots(
    tmp_path,
):
    database = ArchiveDatabase(tmp_path / "dashboard-markers.sqlite3")
    database.initialize()

    database.upsert_vehicle("car-shared", displayed_title="Audi A6")
    database.upsert_auction_lot("car-shared", "auction-fixed")
    database.upsert_auction_lot("car-shared", "auction-other")
    database.upsert_auction(
        "auction-fixed",
        source_auction_name="Our Stock",
        markers=(
            AuctionCardMarker(
                "euro-sign.svg",
                "fixed_price_option",
                "Фиксированная цена автомобилей",
            ),
            AuctionCardMarker(
                "favorite.svg",
                "recommendation",
                "Аукцион рекомендован Exleasingcar",
            ),
        ),
    )
    database.upsert_auction(
        "auction-other",
        source_auction_name="CarNext",
        markers=(AuctionCardMarker("custom.svg", "other", None),),
    )

    database.upsert_vehicle("car-no-markers", displayed_title="BMW 530d")
    database.upsert_auction_lot("car-no-markers", "auction-empty")
    database.upsert_auction(
        "auction-empty",
        markers=(
            AuctionCardMarker("retired.svg", "other", "Устаревший маркер"),
        ),
    )
    # A later observation with an empty marker set removes it from the current view,
    # while retaining its historical row in the database.
    database.upsert_auction("auction-empty", markers=())

    handler = object.__new__(DashboardHandler)
    handler.database_path = database.path

    payload = handler._load_data(selected_car_id="car-shared")
    assert payload["schema_version"] == 8
    markers_by_auction = {
        lot["auction_id"]: lot["markers"] for lot in payload["lots"]
    }

    assert len(markers_by_auction["auction-fixed"]) == 2
    assert {
        (
            marker["icon_filename"],
            marker["marker_group"],
            marker["label"],
        )
        for marker in markers_by_auction["auction-fixed"]
    } == {
        (
            "euro-sign.svg",
            "fixed_price_option",
            "Фиксированная цена автомобилей",
        ),
        (
            "favorite.svg",
            "recommendation",
            "Аукцион рекомендован Exleasingcar",
        ),
    }
    assert markers_by_auction["auction-other"] == [
        {
            "icon_filename": "custom.svg",
            "marker_group": "other",
            "label": None,
        }
    ]

    empty_payload = handler._load_data(selected_car_id="car-no-markers")
    assert empty_payload["lots"][0]["markers"] == []
    with sqlite3.connect(database.path) as connection:
        historical_marker = connection.execute(
            """
            SELECT is_present
            FROM auction_markers
            WHERE icon_filename = 'retired.svg'
            """
        ).fetchone()
    assert historical_marker == (0,)



def test_dashboard_paginates_full_archive_and_search_results(tmp_path):
    database = ArchiveDatabase(tmp_path / "dashboard-pagination.sqlite3")
    database.initialize()
    database.upsert_auction("auction-pagination", source_auction_name="Test auction")

    with sqlite3.connect(database.path) as connection:
        auction_id = connection.execute(
            """
            SELECT id FROM auctions
            WHERE exleasingcar_auction_id = 'auction-pagination'
            """
        ).fetchone()[0]
        for index in range(501):
            car_id = f"car-{index:04d}"
            cursor = connection.execute(
                """
                INSERT INTO vehicles (
                    source_car_id, displayed_title, first_seen_at, last_seen_at
                ) VALUES (?, 'Target vehicle', '2026-01-01T00:00:00Z',
                          '2026-01-01T00:00:00Z')
                """,
                (car_id,),
            )
            connection.execute(
                """
                INSERT INTO auction_lots (
                    vehicle_id, auction_id, first_seen_at, last_seen_at
                ) VALUES (?, ?, '2026-01-01T00:00:00Z',
                          '2026-01-01T00:00:00Z')
                """,
                (cursor.lastrowid, auction_id),
            )

    handler = object.__new__(DashboardHandler)
    handler.database_path = database.path

    first_page = handler._load_data()
    second_page = handler._load_data(page=2)

    assert len(first_page["lots"]) == 500
    assert len(second_page["lots"]) == 1
    assert first_page["pagination"] == {
        "page": 1,
        "page_size": 500,
        "total_lots": 501,
        "total_pages": 2,
        "has_previous": False,
        "has_next": True,
    }
    assert second_page["pagination"]["has_previous"] is True
    assert second_page["pagination"]["has_next"] is False
    first_ids = {lot["car_id"] for lot in first_page["lots"]}
    second_ids = {lot["car_id"] for lot in second_page["lots"]}
    assert first_ids.isdisjoint(second_ids)
    assert second_ids == {"car-0000"}

    searched_second_page = handler._load_data(
        search_query="Target vehicle",
        page=2,
    )
    assert len(searched_second_page["lots"]) == 1
    assert searched_second_page["lots"][0]["car_id"] == "car-0000"
    assert searched_second_page["pagination"]["total_lots"] == 501

    # A page beyond the end is safely clamped to the last available page.
    clamped = handler._load_data(page=10)
    assert clamped["pagination"]["page"] == 2
    assert {lot["car_id"] for lot in clamped["lots"]} == second_ids

def test_dashboard_make_filter_combines_with_search_and_pagination(tmp_path, monkeypatch):
    from exleasingcar_archive import dashboard as dashboard_module

    monkeypatch.setattr(dashboard_module, "LOT_PAGE_SIZE", 1)
    database = ArchiveDatabase(tmp_path / "dashboard-make-filter.sqlite3")
    database.initialize()
    vehicles = (
        ("ford-focus-1", "Ford", "Ford Focus Mk1"),
        ("ford-focus-2", "ford", "Ford Focus Mk2"),
        ("ford-fiesta", "Ford", "Ford Fiesta"),
        ("bmw-focus", "BMW", "BMW Focus"),
        ("unknown-focus", None, "Unknown Focus"),
    )
    for car_id, make, title in vehicles:
        database.upsert_vehicle(car_id, make=make, displayed_title=title)
        database.upsert_auction_lot(car_id, "auction-make-filter")

    handler = object.__new__(DashboardHandler)
    handler.database_path = database.path

    first_page = handler._load_data(
        search_query="Focus",
        selected_make="Ford",
        page=1,
    )
    second_page = handler._load_data(
        search_query="Focus",
        selected_make="Ford",
        page=2,
    )
    clamped_page = handler._load_data(
        search_query="Focus",
        selected_make="Ford",
        page=99,
    )

    assert first_page["available_makes"] == ["BMW", "Ford"]
    assert first_page["selected_make"] == "Ford"
    assert first_page["pagination"]["total_lots"] == 2
    assert first_page["pagination"]["total_pages"] == 2
    assert first_page["pagination"]["has_next"] is True
    assert second_page["pagination"]["has_previous"] is True
    assert len(first_page["lots"]) == 1
    assert len(second_page["lots"]) == 1
    assert {first_page["lots"][0]["car_id"], second_page["lots"][0]["car_id"]} == {
        "ford-focus-1",
        "ford-focus-2",
    }
    assert clamped_page["pagination"]["page"] == 2
    assert clamped_page["lots"][0]["car_id"] == "ford-focus-1"


def test_dashboard_selected_car_keeps_full_history_when_make_filter_is_present(
    tmp_path,
):
    database = ArchiveDatabase(tmp_path / "dashboard-selected-car-make-filter.sqlite3")
    database.initialize()
    database.upsert_vehicle(
        "car-audi",
        make="Audi",
        displayed_title="Audi A6",
    )
    database.upsert_auction_lot("car-audi", "auction-old")
    database.upsert_auction_lot("car-audi", "auction-new")
    database.upsert_vehicle(
        "car-bmw",
        make="BMW",
        displayed_title="BMW 530d",
    )
    database.upsert_auction_lot("car-bmw", "auction-bmw")

    handler = object.__new__(DashboardHandler)
    handler.database_path = database.path
    payload = handler._load_data(
        selected_car_id="car-audi",
        selected_make="BMW",
    )

    assert payload["selected_vehicle"]["make"] == "Audi"
    assert {lot["auction_id"] for lot in payload["lots"]} == {
        "auction-old",
        "auction-new",
    }
    assert payload["pagination"]["total_lots"] == 2

def test_dashboard_labels_unverified_highest_offer_as_api_value():
    from exleasingcar_archive.dashboard import PAGE

    assert "История значений API highest_offer" in PAGE
    assert "Смысл этого поля пока не подтверждён" in PAGE
    assert "API highest_offer" in PAGE
    assert "История реальных ставок" not in PAGE
    assert "Последняя ставка" not in PAGE
    assert "Максимальная ставка" not in PAGE



def test_dashboard_copy_includes_zero_highest_offer_observations():
    assert "включая 0" in PAGE
    assert "только положительные значения" not in PAGE
    assert "Наблюдений API highest_offer пока нет" in PAGE
