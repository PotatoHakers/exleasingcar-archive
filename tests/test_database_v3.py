import sqlite3

from exleasingcar_archive.database import (
    AUCTION_EVENTS_SQL,
    AUCTION_LOTS_SQL,
    AUCTIONS_SQL,
    PRICE_OBSERVATIONS_SQL,
    SCHEMA_VERSION,
    VEHICLES_SQL,
    ArchiveDatabase,
)


def test_v2_migration_adds_v4_metadata_and_preserves_history(tmp_path):
    database_path = tmp_path / "archive_v2.sqlite3"
    vehicles_v2_sql = VEHICLES_SQL.replace("    displayed_title TEXT,\n", "")
    vehicles_v2_sql = vehicles_v2_sql.replace("    chassis_number TEXT,\n", "")
    vehicles_v2_sql = vehicles_v2_sql.replace("    chassis_number_fragment TEXT,\n", "")
    vehicles_v2_sql = vehicles_v2_sql.replace("    chassis_number_masked INTEGER,\n", "")
    auction_lots_v2_sql = AUCTION_LOTS_SQL.replace("    mileage_km INTEGER,\n", "")

    with sqlite3.connect(database_path) as connection:
        for statement in (
            AUCTIONS_SQL,
            vehicles_v2_sql,
            auction_lots_v2_sql,
            PRICE_OBSERVATIONS_SQL,
            AUCTION_EVENTS_SQL,
        ):
            connection.execute(statement)

        connection.execute(
            """
            INSERT INTO auctions (
                exleasingcar_auction_id, first_seen_at, last_seen_at
            ) VALUES ('210547', '2026-11-12T12:00:00Z', '2026-11-12T12:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO vehicles (source_car_id, first_seen_at, last_seen_at)
            VALUES ('19702473', '2026-11-12T12:00:00Z', '2026-11-12T12:00:00Z')
            """
        )
        vehicle_id = connection.execute(
            "SELECT id FROM vehicles WHERE source_car_id = '19702473'"
        ).fetchone()[0]
        auction_id = connection.execute(
            "SELECT id FROM auctions WHERE exleasingcar_auction_id = '210547'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO auction_lots (
                vehicle_id, auction_id, first_seen_at, last_seen_at
            ) VALUES (?, ?, '2026-11-12T12:00:00Z', '2026-11-12T12:00:00Z')
            """,
            (vehicle_id, auction_id),
        )
        lot_id = connection.execute(
            "SELECT id FROM auction_lots"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO price_observations (
                auction_lot_id, highest_offer, observed_at, source
            ) VALUES (?, '18300', '2026-11-12T12:00:00Z', 'test')
            """,
            (lot_id,),
        )
        connection.execute("PRAGMA user_version = 2")

    database = ArchiveDatabase(database_path)
    database.initialize()

    with sqlite3.connect(database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        title = connection.execute(
            "SELECT displayed_title FROM vehicles WHERE source_car_id = '19702473'"
        ).fetchone()[0]
        price = connection.execute(
            "SELECT highest_offer FROM price_observations"
        ).fetchone()[0]

    assert version == SCHEMA_VERSION == 4
    assert title is None
    assert price == "18300"
    assert connection.execute(
        "SELECT COUNT(*) FROM price_reference_observations"
    ).fetchone()[0] == 0

    database.upsert_vehicle(
        "19702473",
        displayed_title="Mercedes-Benz E-KLASSE - 220 d",
    )
    with sqlite3.connect(database_path) as connection:
        title = connection.execute(
            "SELECT displayed_title FROM vehicles WHERE source_car_id = '19702473'"
        ).fetchone()[0]
    assert title == "Mercedes-Benz E-KLASSE - 220 d"


def test_vehicle_title_is_upserted_without_erasing_existing_value(tmp_path):
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()

    database.upsert_vehicle("19702473", displayed_title="Mercedes-Benz E-KLASSE")
    database.upsert_vehicle("19702473", location_country_code="DE")

    with sqlite3.connect(database.path) as connection:
        row = connection.execute(
            "SELECT displayed_title, location_country_code FROM vehicles"
        ).fetchone()

    assert row == ("Mercedes-Benz E-KLASSE", "DE")


def test_v3_migration_adds_vehicle_identity_fields_and_reference_table(tmp_path):
    database_path = tmp_path / "archive_v3.sqlite3"
    vehicles_v3_sql = VEHICLES_SQL.replace("    chassis_number TEXT,\n", "")
    vehicles_v3_sql = vehicles_v3_sql.replace("    chassis_number_fragment TEXT,\n", "")
    vehicles_v3_sql = vehicles_v3_sql.replace("    chassis_number_masked INTEGER,\n", "")
    lots_v3_sql = AUCTION_LOTS_SQL.replace("    mileage_km INTEGER,\n", "")

    with sqlite3.connect(database_path) as connection:
        for statement in (
            AUCTIONS_SQL,
            vehicles_v3_sql,
            lots_v3_sql,
            PRICE_OBSERVATIONS_SQL,
            AUCTION_EVENTS_SQL,
        ):
            connection.execute(statement)
        connection.execute(
            "INSERT INTO vehicles (source_car_id, first_seen_at, last_seen_at) "
            "VALUES ('old-car', '2026-11-13T00:00:00Z', '2026-11-13T00:00:00Z')"
        )
        connection.execute("PRAGMA user_version = 3")

    database = ArchiveDatabase(database_path)
    database.initialize()
    database.upsert_vehicle(
        "old-car",
        chassis_number_fragment="ABCD1234",
        chassis_number_masked=True,
    )
    database.upsert_auction_lot("old-car", "auction-1", mileage_km=74002)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        identity = connection.execute(
            "SELECT chassis_number, chassis_number_fragment, chassis_number_masked "
            "FROM vehicles WHERE source_car_id = 'old-car'"
        ).fetchone()
        mileage = connection.execute(
            "SELECT mileage_km FROM auction_lots"
        ).fetchone()[0]

    assert identity == (None, "ABCD1234", 1)
    assert mileage == 74002
