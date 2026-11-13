import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from exleasingcar_archive.database import SCHEMA_VERSION, ArchiveDatabase

UTC_FIRST = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
UTC_SECOND = datetime(2026, 11, 12, 12, 5, tzinfo=timezone.utc)
UTC_THIRD = datetime(2026, 11, 12, 12, 10, tzinfo=timezone.utc)


def test_database_initialization_creates_v2_schema(tmp_path):
    database = ArchiveDatabase(tmp_path / "test_archive.sqlite3")
    database.initialize()

    with sqlite3.connect(database.path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert version == SCHEMA_VERSION
    assert {
        "auctions",
        "vehicles",
        "auction_lots",
        "price_observations",
        "auction_events",
    }.issubset(tables)


def test_auction_and_vehicle_metadata_are_upserted(tmp_path):
    database = ArchiveDatabase(tmp_path / "test_archive.sqlite3")
    database.initialize()

    database.upsert_auction(
        "210547",
        source_auction_name="BCA DE 3.3",
        source_auction_country_code="DE",
        source_auction_country_name="Германия",
        auction_type_label="Английский аукцион",
        auction_status_label="FINAL CALL",
        observed_at=UTC_FIRST,
    )
    database.upsert_vehicle(
        "19702473",
        make="Mercedes-Benz",
        model="E-KLASSE 220 d 4Matic T",
        location_text="DE-04509, Германия",
        location_country_code="DE",
        location_country_name="Германия",
        last_registration_country_code="DE",
        last_registration_country_name="Германия",
        observed_at=UTC_FIRST,
    )

    with sqlite3.connect(database.path) as connection:
        auction = connection.execute(
            """
            SELECT exleasingcar_auction_id, source_auction_name,
                   source_auction_country_code, source_auction_country_name,
                   auction_type_label, auction_status_label
            FROM auctions
            """
        ).fetchone()
        vehicle = connection.execute(
            """
            SELECT source_car_id, make, model, location_country_code,
                   last_registration_country_code
            FROM vehicles
            """
        ).fetchone()

    assert auction == (
        "210547",
        "BCA DE 3.3",
        "DE",
        "Германия",
        "Английский аукцион",
        "FINAL CALL",
    )
    assert vehicle == (
        "19702473",
        "Mercedes-Benz",
        "E-KLASSE 220 d 4Matic T",
        "DE",
        "DE",
    )


def test_auction_lot_connects_vehicle_and_exleasingcar_auction(tmp_path):
    database = ArchiveDatabase(tmp_path / "test_archive.sqlite3")
    database.initialize()

    lot_id = database.upsert_auction_lot(
        "19702473",
        "210547",
        displayed_lot_reference="2258673/41",
        listing_code_attribute="2352500179",
        observed_at=UTC_FIRST,
    )

    with sqlite3.connect(database.path) as connection:
        row = connection.execute(
            """
            SELECT v.source_car_id, a.exleasingcar_auction_id,
                   l.displayed_lot_reference, l.listing_code_attribute
            FROM auction_lots AS l
            JOIN vehicles AS v ON v.id = l.vehicle_id
            JOIN auctions AS a ON a.id = l.auction_id
            WHERE l.id = ?
            """,
            (lot_id,),
        ).fetchone()

    assert row == ("19702473", "210547", "2258673/41", "2352500179")


def test_price_observations_are_scoped_to_auction_lot(tmp_path):
    database = ArchiveDatabase(tmp_path / "test_archive.sqlite3")
    database.initialize()

    assert database.record_price_observation(
        "19702473",
        Decimal(18300),
        UTC_FIRST,
        exleasingcar_auction_id="210547",
    )
    assert not database.record_price_observation(
        "19702473",
        Decimal(18300),
        UTC_SECOND,
        exleasingcar_auction_id="210547",
    )
    assert database.record_price_observation(
        "19702473",
        Decimal(18400),
        UTC_THIRD,
        exleasingcar_auction_id="210547",
    )

    with sqlite3.connect(database.path) as connection:
        rows = connection.execute(
            """
            SELECT p.highest_offer, a.exleasingcar_auction_id
            FROM price_observations AS p
            JOIN auction_lots AS l ON l.id = p.auction_lot_id
            JOIN auctions AS a ON a.id = l.auction_id
            ORDER BY p.id
            """
        ).fetchall()

    assert rows == [("18300", "210547"), ("18400", "210547")]


def test_same_vehicle_in_two_auctions_has_separate_lot_histories(tmp_path):
    database = ArchiveDatabase(tmp_path / "test_archive.sqlite3")
    database.initialize()

    assert database.record_price_observation(
        "19702473", Decimal(18300), exleasingcar_auction_id="210547"
    )
    assert database.record_price_observation(
        "19702473", Decimal(17500), exleasingcar_auction_id="210600"
    )

    with sqlite3.connect(database.path) as connection:
        rows = connection.execute(
            """
            SELECT a.exleasingcar_auction_id, p.highest_offer
            FROM price_observations AS p
            JOIN auction_lots AS l ON l.id = p.auction_lot_id
            JOIN auctions AS a ON a.id = l.auction_id
            ORDER BY a.exleasingcar_auction_id
            """
        ).fetchall()

        vehicle_count = connection.execute(
            "SELECT COUNT(*) FROM vehicles WHERE source_car_id = '19702473'"
        ).fetchone()[0]

    assert rows == [("210547", "18300"), ("210600", "17500")]
    assert vehicle_count == 1


def test_price_can_return_to_previous_value(tmp_path):
    database = ArchiveDatabase(tmp_path / "test_archive.sqlite3")
    database.initialize()

    for price in (18300, 18400, 18300):
        assert database.record_price_observation(
            "19702473", Decimal(price), exleasingcar_auction_id="210547"
        )

    with sqlite3.connect(database.path) as connection:
        rows = connection.execute(
            "SELECT highest_offer FROM price_observations ORDER BY id"
        ).fetchall()

    assert rows == [("18300",), ("18400",), ("18300",)]


def test_auction_event_ignores_timer_only_change(tmp_path):
    database = ArchiveDatabase(tmp_path / "test_archive.sqlite3")
    database.initialize()

    assert database.record_auction_event(
        "19702473",
        auction_phase="final-call",
        final_call_active=True,
        live_bidding=False,
        remaining_seconds=20,
        observed_at=UTC_FIRST,
        exleasingcar_auction_id="210547",
    )
    assert not database.record_auction_event(
        "19702473",
        auction_phase="final-call",
        final_call_active=True,
        live_bidding=False,
        remaining_seconds=15,
        observed_at=UTC_SECOND,
        exleasingcar_auction_id="210547",
    )

    with sqlite3.connect(database.path) as connection:
        rows = connection.execute(
            "SELECT auction_phase, remaining_seconds FROM auction_events"
        ).fetchall()

    assert rows == [("final-call", 20)]


def test_auction_state_change_creates_event(tmp_path):
    database = ArchiveDatabase(tmp_path / "test_archive.sqlite3")
    database.initialize()

    assert database.record_auction_event(
        "19702473",
        auction_phase="final-call",
        final_call_active=True,
        exleasingcar_auction_id="210547",
    )
    assert database.record_auction_event(
        "19702473",
        auction_phase="completed",
        final_call_active=False,
        bid_status="inactive",
        exleasingcar_auction_id="210547",
    )

    with sqlite3.connect(database.path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM auction_events"
        ).fetchone()[0]

    assert count == 2


def test_zero_price_is_preserved(tmp_path):
    database = ArchiveDatabase(tmp_path / "test_archive.sqlite3")
    database.initialize()

    assert database.record_price_observation(
        "19702473", Decimal(0), exleasingcar_auction_id="210547"
    )

    with sqlite3.connect(database.path) as connection:
        price = connection.execute(
            "SELECT highest_offer FROM price_observations"
        ).fetchone()[0]

    assert price == "0"


def test_naive_datetime_is_rejected(tmp_path):
    database = ArchiveDatabase(tmp_path / "test_archive.sqlite3")
    database.initialize()

    with pytest.raises(ValueError, match="timezone-aware"):
        database.record_price_observation(
            "19702473",
            Decimal(18300),
            datetime(2026, 11, 12, 12, 0),
        )


def test_v1_migration_preserves_existing_price_and_event_history(tmp_path):
    database_path = tmp_path / "legacy_v1.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE vehicles (
                id INTEGER PRIMARY KEY,
                source_car_id TEXT NOT NULL UNIQUE,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE TABLE price_observations (
                id INTEGER PRIMARY KEY,
                vehicle_id INTEGER NOT NULL,
                highest_offer TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                source TEXT NOT NULL,
                FOREIGN KEY (vehicle_id) REFERENCES vehicles(id) ON DELETE RESTRICT
            );
            CREATE TABLE auction_events (
                id INTEGER PRIMARY KEY,
                vehicle_id INTEGER NOT NULL,
                auction_phase TEXT,
                bid_status TEXT,
                final_call_active INTEGER,
                live_bidding INTEGER,
                auction_end_at TEXT,
                remaining_seconds INTEGER,
                observed_at TEXT NOT NULL,
                source TEXT NOT NULL,
                FOREIGN KEY (vehicle_id) REFERENCES vehicles(id) ON DELETE RESTRICT
            );
            CREATE INDEX idx_price_observations_vehicle_time
                ON price_observations(vehicle_id, observed_at);
            CREATE INDEX idx_auction_events_vehicle_time
                ON auction_events(vehicle_id, observed_at);
            INSERT INTO vehicles VALUES (
                1, '19702473',
                '2026-11-12T12:00:00.000000Z',
                '2026-11-12T12:10:00.000000Z'
            );
            INSERT INTO price_observations VALUES (
                1, 1, '18300', '2026-11-12T12:00:00.000000Z',
                'ajax_update_car_form.v3.1.php'
            );
            INSERT INTO auction_events VALUES (
                1, 1, 'final-call', NULL, 1, 0, NULL, 20,
                '2026-11-12T12:00:00.000000Z',
                'ajax_update_car_form.v3.1.php'
            );
            PRAGMA user_version = 1;
            """
        )

    database = ArchiveDatabase(database_path)
    database.initialize()

    with sqlite3.connect(database.path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        price = connection.execute(
            """
            SELECT p.highest_offer, v.source_car_id, l.auction_id
            FROM price_observations AS p
            JOIN auction_lots AS l ON l.id = p.auction_lot_id
            JOIN vehicles AS v ON v.id = l.vehicle_id
            """
        ).fetchone()
        event = connection.execute(
            """
            SELECT e.auction_phase, e.remaining_seconds, l.auction_id
            FROM auction_events AS e
            JOIN auction_lots AS l ON l.id = e.auction_lot_id
            """
        ).fetchone()
        foreign_key_errors = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

    assert version == SCHEMA_VERSION
    assert price == ("18300", "19702473", None)
    assert event == ("final-call", 20, None)
    assert foreign_key_errors == []


def test_newer_database_version_is_rejected(tmp_path):
    database_path = tmp_path / "future.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version = 99")

    database = ArchiveDatabase(database_path)
    with pytest.raises(RuntimeError, match="newer than supported"):
        database.initialize()
