import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

SCHEMA_VERSION = 2
DEFAULT_SOURCE = "ajax_update_car_form.v3.1.php"


AUCTIONS_SQL = """
CREATE TABLE IF NOT EXISTS auctions (
    id INTEGER PRIMARY KEY,
    exleasingcar_auction_id TEXT NOT NULL UNIQUE,
    source_auction_name TEXT,
    source_auction_country_code TEXT,
    source_auction_country_name TEXT,
    auction_type_label TEXT,
    auction_status_label TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
)
"""

VEHICLES_SQL = """
CREATE TABLE IF NOT EXISTS vehicles (
    id INTEGER PRIMARY KEY,
    source_car_id TEXT NOT NULL UNIQUE,
    make TEXT,
    model TEXT,
    location_text TEXT,
    location_country_code TEXT,
    location_country_name TEXT,
    last_registration_country_code TEXT,
    last_registration_country_name TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
)
"""

AUCTION_LOTS_SQL = """
CREATE TABLE IF NOT EXISTS auction_lots (
    id INTEGER PRIMARY KEY,
    vehicle_id INTEGER NOT NULL,
    auction_id INTEGER,
    displayed_lot_reference TEXT,
    listing_code_attribute TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    FOREIGN KEY (vehicle_id) REFERENCES vehicles(id) ON DELETE RESTRICT,
    FOREIGN KEY (auction_id) REFERENCES auctions(id) ON DELETE RESTRICT,
    UNIQUE (vehicle_id, auction_id)
)
"""

PRICE_OBSERVATIONS_SQL = """
CREATE TABLE IF NOT EXISTS price_observations (
    id INTEGER PRIMARY KEY,
    auction_lot_id INTEGER NOT NULL,
    highest_offer TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    FOREIGN KEY (auction_lot_id) REFERENCES auction_lots(id) ON DELETE RESTRICT
)
"""

AUCTION_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS auction_events (
    id INTEGER PRIMARY KEY,
    auction_lot_id INTEGER NOT NULL,
    auction_phase TEXT,
    bid_status TEXT,
    final_call_active INTEGER,
    live_bidding INTEGER,
    auction_end_at TEXT,
    remaining_seconds INTEGER,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    FOREIGN KEY (auction_lot_id) REFERENCES auction_lots(id) ON DELETE RESTRICT
)
"""


class ArchiveDatabase:
    """SQLite storage for Exleasingcar Archive."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create schema v2 or safely migrate an existing v1 database."""
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with self._connect() as connection:
            # Keep schema creation and migration atomic if any DDL/DML statement fails.
            connection.execute("BEGIN IMMEDIATE")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database version {version} is newer than supported "
                    f"version {SCHEMA_VERSION}"
                )

            if version == 0:
                self._create_schema(connection)
            elif version == 1:
                self._migrate_v1_to_v2(connection)
                self._create_indexes(connection)
            else:
                # Idempotently ensure indexes exist for an already initialized DB.
                self._create_indexes(connection)

            connection.execute(
                f"PRAGMA user_version = {SCHEMA_VERSION}"
            )

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        for statement in (
            AUCTIONS_SQL,
            VEHICLES_SQL,
            AUCTION_LOTS_SQL,
            PRICE_OBSERVATIONS_SQL,
            AUCTION_EVENTS_SQL,
        ):
            connection.execute(statement)
        ArchiveDatabase._create_indexes(connection)

    
    @staticmethod
    def _create_indexes(connection: sqlite3.Connection) -> None:
        statements = (
            """
            CREATE INDEX IF NOT EXISTS idx_auction_lots_auction_vehicle
            ON auction_lots(auction_id, vehicle_id)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_auction_lots_unassigned_vehicle
            ON auction_lots(vehicle_id)
            WHERE auction_id IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_price_observations_lot_time
            ON price_observations(auction_lot_id, observed_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_auction_events_lot_time
            ON auction_events(auction_lot_id, observed_at)
            """,
        )

        for statement in statements:
            connection.execute(statement)


    @staticmethod
    def _add_column_if_missing(
        connection: sqlite3.Connection,
        table_name: str,
        column_name: str,
        column_definition: str,
    ) -> None:
        columns = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table_name})")
        }
        if column_name not in columns:
            connection.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} "
                f"{column_definition}"
            )

    @classmethod
    def _migrate_v1_to_v2(cls, connection: sqlite3.Connection) -> None:
        """Move v1 per-vehicle observations to per-auction-lot observations."""
        # Add metadata columns to the existing vehicle records without discarding them.
        for name, definition in (
            ("make", "TEXT"),
            ("model", "TEXT"),
            ("location_text", "TEXT"),
            ("location_country_code", "TEXT"),
            ("location_country_name", "TEXT"),
            ("last_registration_country_code", "TEXT"),
            ("last_registration_country_name", "TEXT"),
        ):
            ArchiveDatabase._add_column_if_missing(
                connection, "vehicles", name, definition
            )

        connection.execute(AUCTIONS_SQL)
        connection.execute(AUCTION_LOTS_SQL)

        # Existing observations did not carry an auction ID. Preserve them on an
        # explicitly unassigned lot instead of inventing an auction relationship.
        connection.execute(
            """
            INSERT INTO auction_lots (
                vehicle_id, auction_id, first_seen_at, last_seen_at
            )
            SELECT id, NULL, first_seen_at, last_seen_at
            FROM vehicles
            """
        )

        connection.execute(
            """
            CREATE TABLE price_observations_v2 (
                id INTEGER PRIMARY KEY,
                auction_lot_id INTEGER NOT NULL,
                highest_offer TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                source TEXT NOT NULL,
                FOREIGN KEY (auction_lot_id)
                    REFERENCES auction_lots(id) ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO price_observations_v2 (
                id, auction_lot_id, highest_offer, observed_at, source
            )
            SELECT p.id, l.id, p.highest_offer, p.observed_at, p.source
            FROM price_observations AS p
            JOIN auction_lots AS l
              ON l.vehicle_id = p.vehicle_id AND l.auction_id IS NULL
            """
        )

        connection.execute(
            """
            CREATE TABLE auction_events_v2 (
                id INTEGER PRIMARY KEY,
                auction_lot_id INTEGER NOT NULL,
                auction_phase TEXT,
                bid_status TEXT,
                final_call_active INTEGER,
                live_bidding INTEGER,
                auction_end_at TEXT,
                remaining_seconds INTEGER,
                observed_at TEXT NOT NULL,
                source TEXT NOT NULL,
                FOREIGN KEY (auction_lot_id)
                    REFERENCES auction_lots(id) ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO auction_events_v2 (
                id, auction_lot_id, auction_phase, bid_status,
                final_call_active, live_bidding, auction_end_at,
                remaining_seconds, observed_at, source
            )
            SELECT
                e.id, l.id, e.auction_phase, e.bid_status,
                e.final_call_active, e.live_bidding, e.auction_end_at,
                e.remaining_seconds, e.observed_at, e.source
            FROM auction_events AS e
            JOIN auction_lots AS l
              ON l.vehicle_id = e.vehicle_id AND l.auction_id IS NULL
            """
        )

        connection.execute("DROP TABLE price_observations")
        connection.execute("DROP TABLE auction_events")
        connection.execute(
            "ALTER TABLE price_observations_v2 RENAME TO price_observations"
        )
        connection.execute("ALTER TABLE auction_events_v2 RENAME TO auction_events")

    @staticmethod
    def _serialize_datetime(value: datetime | None) -> str:
        if value is None:
            value = datetime.now(timezone.utc)
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return (
            value.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _serialize_decimal(value: Decimal) -> str:
        if not isinstance(value, Decimal):
            raise TypeError("highest_offer must be Decimal")
        if not value.is_finite():
            raise ValueError("highest_offer must be finite")
        return format(value, "f")

    @staticmethod
    def _serialize_bool(value: bool | None) -> int | None:
        if value is None:
            return None
        if not isinstance(value, bool):
            raise TypeError("Boolean fields must be bool or None")
        return int(value)

    @staticmethod
    def _validate_identifier(value: str, field_name: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{field_name} must be str")
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{field_name} cannot be empty")
        return normalized

    @staticmethod
    def _ensure_vehicle(
        connection: sqlite3.Connection,
        source_car_id: str,
        observed_at: str,
        *,
        make: str | None = None,
        model: str | None = None,
        location_text: str | None = None,
        location_country_code: str | None = None,
        location_country_name: str | None = None,
        last_registration_country_code: str | None = None,
        last_registration_country_name: str | None = None,
    ) -> int:
        connection.execute(
            """
            INSERT INTO vehicles (
                source_car_id, make, model, location_text,
                location_country_code, location_country_name,
                last_registration_country_code, last_registration_country_name,
                first_seen_at, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_car_id) DO UPDATE SET
                first_seen_at = MIN(vehicles.first_seen_at, excluded.first_seen_at),
                last_seen_at = MAX(vehicles.last_seen_at, excluded.last_seen_at),
                make = COALESCE(excluded.make, vehicles.make),
                model = COALESCE(excluded.model, vehicles.model),
                location_text = COALESCE(
                    excluded.location_text, vehicles.location_text
                ),
                location_country_code = COALESCE(
                    excluded.location_country_code, vehicles.location_country_code
                ),
                location_country_name = COALESCE(
                    excluded.location_country_name, vehicles.location_country_name
                ),
                last_registration_country_code = COALESCE(
                    excluded.last_registration_country_code,
                    vehicles.last_registration_country_code
                ),
                last_registration_country_name = COALESCE(
                    excluded.last_registration_country_name,
                    vehicles.last_registration_country_name
                )
            """,
            (
                source_car_id,
                make,
                model,
                location_text,
                location_country_code,
                location_country_name,
                last_registration_country_code,
                last_registration_country_name,
                observed_at,
                observed_at,
            ),
        )
        row = connection.execute(
            "SELECT id FROM vehicles WHERE source_car_id = ?",
            (source_car_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Failed to create or find vehicle")
        return int(row["id"])

    @staticmethod
    def _ensure_auction(
        connection: sqlite3.Connection,
        exleasingcar_auction_id: str,
        observed_at: str,
        *,
        source_auction_name: str | None = None,
        source_auction_country_code: str | None = None,
        source_auction_country_name: str | None = None,
        auction_type_label: str | None = None,
        auction_status_label: str | None = None,
    ) -> int:
        connection.execute(
            """
            INSERT INTO auctions (
                exleasingcar_auction_id, source_auction_name,
                source_auction_country_code, source_auction_country_name,
                auction_type_label, auction_status_label,
                first_seen_at, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(exleasingcar_auction_id) DO UPDATE SET
                first_seen_at = MIN(auctions.first_seen_at, excluded.first_seen_at),
                last_seen_at = MAX(auctions.last_seen_at, excluded.last_seen_at),
                source_auction_name = COALESCE(
                    excluded.source_auction_name, auctions.source_auction_name
                ),
                source_auction_country_code = COALESCE(
                    excluded.source_auction_country_code,
                    auctions.source_auction_country_code
                ),
                source_auction_country_name = COALESCE(
                    excluded.source_auction_country_name,
                    auctions.source_auction_country_name
                ),
                auction_type_label = COALESCE(
                    excluded.auction_type_label, auctions.auction_type_label
                ),
                auction_status_label = COALESCE(
                    excluded.auction_status_label, auctions.auction_status_label
                )
            """,
            (
                exleasingcar_auction_id,
                source_auction_name,
                source_auction_country_code,
                source_auction_country_name,
                auction_type_label,
                auction_status_label,
                observed_at,
                observed_at,
            ),
        )
        row = connection.execute(
            "SELECT id FROM auctions WHERE exleasingcar_auction_id = ?",
            (exleasingcar_auction_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Failed to create or find auction")
        return int(row["id"])

    @staticmethod
    def _ensure_auction_lot(
        connection: sqlite3.Connection,
        vehicle_id: int,
        auction_id: int | None,
        observed_at: str,
        *,
        displayed_lot_reference: str | None = None,
        listing_code_attribute: str | None = None,
    ) -> int:
        if auction_id is None:
            row = connection.execute(
                "SELECT id FROM auction_lots "
            "WHERE vehicle_id = ? AND auction_id IS NULL",
                (vehicle_id,),
            ).fetchone()
            if row is not None:
                lot_id = int(row["id"])
                connection.execute(
                    """
                    UPDATE auction_lots SET
                        first_seen_at = MIN(first_seen_at, ?),
                        last_seen_at = MAX(last_seen_at, ?),
                        displayed_lot_reference = COALESCE(?, displayed_lot_reference),
                        listing_code_attribute = COALESCE(?, listing_code_attribute)
                    WHERE id = ?
                    """,
                    (
                        observed_at,
                        observed_at,
                        displayed_lot_reference,
                        listing_code_attribute,
                        lot_id,
                    ),
                )
                return lot_id
            connection.execute(
                """
                INSERT INTO auction_lots (
                    vehicle_id, auction_id, displayed_lot_reference,
                    listing_code_attribute, first_seen_at, last_seen_at
                ) VALUES (?, NULL, ?, ?, ?, ?)
                """,
                (
                    vehicle_id,
                    displayed_lot_reference,
                    listing_code_attribute,
                    observed_at,
                    observed_at,
                ),
            )
        else:
            connection.execute(
                """
                INSERT INTO auction_lots (
                    vehicle_id, auction_id, displayed_lot_reference,
                    listing_code_attribute, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(vehicle_id, auction_id) DO UPDATE SET
                    first_seen_at = MIN(
                        auction_lots.first_seen_at, excluded.first_seen_at
                    ),
                    last_seen_at = MAX(
                        auction_lots.last_seen_at, excluded.last_seen_at
                    ),
                    displayed_lot_reference = COALESCE(
                        excluded.displayed_lot_reference,
                        auction_lots.displayed_lot_reference
                    ),
                    listing_code_attribute = COALESCE(
                        excluded.listing_code_attribute,
                        auction_lots.listing_code_attribute
                    )
                """,
                (
                    vehicle_id,
                    auction_id,
                    displayed_lot_reference,
                    listing_code_attribute,
                    observed_at,
                    observed_at,
                ),
            )
            row = connection.execute(
                "SELECT id FROM auction_lots "
                "WHERE vehicle_id = ? AND auction_id = ?",
                (vehicle_id, auction_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("Failed to create or find auction lot")
            return int(row["id"])

        row = connection.execute(
            "SELECT id FROM auction_lots "
            "WHERE vehicle_id = ? AND auction_id IS NULL",
            (vehicle_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Failed to create or find unassigned auction lot")
        return int(row["id"])

    def upsert_auction(
        self,
        exleasingcar_auction_id: str,
        *,
        source_auction_name: str | None = None,
        source_auction_country_code: str | None = None,
        source_auction_country_name: str | None = None,
        auction_type_label: str | None = None,
        auction_status_label: str | None = None,
        observed_at: datetime | None = None,
    ) -> int:
        auction_id = self._validate_identifier(
            exleasingcar_auction_id, "exleasingcar_auction_id"
        )
        timestamp = self._serialize_datetime(observed_at)
        with self._connect() as connection:
            return self._ensure_auction(
                connection,
                auction_id,
                timestamp,
                source_auction_name=source_auction_name,
                source_auction_country_code=source_auction_country_code,
                source_auction_country_name=source_auction_country_name,
                auction_type_label=auction_type_label,
                auction_status_label=auction_status_label,
            )

    def upsert_vehicle(
        self,
        source_car_id: str,
        *,
        make: str | None = None,
        model: str | None = None,
        location_text: str | None = None,
        location_country_code: str | None = None,
        location_country_name: str | None = None,
        last_registration_country_code: str | None = None,
        last_registration_country_name: str | None = None,
        observed_at: datetime | None = None,
    ) -> int:
        car_id = self._validate_identifier(source_car_id, "source_car_id")
        timestamp = self._serialize_datetime(observed_at)
        with self._connect() as connection:
            return self._ensure_vehicle(
                connection,
                car_id,
                timestamp,
                make=make,
                model=model,
                location_text=location_text,
                location_country_code=location_country_code,
                location_country_name=location_country_name,
                last_registration_country_code=last_registration_country_code,
                last_registration_country_name=last_registration_country_name,
            )

    def upsert_auction_lot(
        self,
        source_car_id: str,
        exleasingcar_auction_id: str | None = None,
        *,
        displayed_lot_reference: str | None = None,
        listing_code_attribute: str | None = None,
        observed_at: datetime | None = None,
    ) -> int:
        car_id = self._validate_identifier(source_car_id, "source_car_id")
        auction_external_id = None
        if exleasingcar_auction_id is not None:
            auction_external_id = self._validate_identifier(
                exleasingcar_auction_id, "exleasingcar_auction_id"
            )
        timestamp = self._serialize_datetime(observed_at)
        with self._connect() as connection:
            vehicle_id = self._ensure_vehicle(connection, car_id, timestamp)
            auction_id = None
            if auction_external_id is not None:
                auction_id = self._ensure_auction(
                    connection, auction_external_id, timestamp
                )
            return self._ensure_auction_lot(
                connection,
                vehicle_id,
                auction_id,
                timestamp,
                displayed_lot_reference=displayed_lot_reference,
                listing_code_attribute=listing_code_attribute,
            )

    def record_price_observation(
        self,
        source_car_id: str,
        highest_offer: Decimal,
        observed_at: datetime | None = None,
        source: str = DEFAULT_SOURCE,
        *,
        exleasingcar_auction_id: str | None = None,
        displayed_lot_reference: str | None = None,
        listing_code_attribute: str | None = None,
    ) -> bool:
        """Store a price against an auction lot or an unassigned legacy lot."""
        car_id = self._validate_identifier(source_car_id, "source_car_id")
        price = self._serialize_decimal(highest_offer)
        timestamp = self._serialize_datetime(observed_at)
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source cannot be empty")

        with self._connect() as connection:
            vehicle_id = self._ensure_vehicle(connection, car_id, timestamp)
            auction_id = None
            if exleasingcar_auction_id is not None:
                auction_external_id = self._validate_identifier(
                    exleasingcar_auction_id, "exleasingcar_auction_id"
                )
                auction_id = self._ensure_auction(
                    connection, auction_external_id, timestamp
                )
            lot_id = self._ensure_auction_lot(
                connection,
                vehicle_id,
                auction_id,
                timestamp,
                displayed_lot_reference=displayed_lot_reference,
                listing_code_attribute=listing_code_attribute,
            )

            latest = connection.execute(
                """
                SELECT highest_offer FROM price_observations
                WHERE auction_lot_id = ? ORDER BY id DESC LIMIT 1
                """,
                (lot_id,),
            ).fetchone()
            if latest is not None and latest["highest_offer"] == price:
                return False

            connection.execute(
                """
                INSERT INTO price_observations (
                    auction_lot_id, highest_offer, observed_at, source
                ) VALUES (?, ?, ?, ?)
                """,
                (lot_id, price, timestamp, source),
            )
        return True

    def record_auction_event(
        self,
        source_car_id: str,
        auction_phase: str | None = None,
        bid_status: str | None = None,
        final_call_active: bool | None = None,
        live_bidding: bool | None = None,
        auction_end_at: str | None = None,
        remaining_seconds: int | None = None,
        observed_at: datetime | None = None,
        source: str = DEFAULT_SOURCE,
        *,
        exleasingcar_auction_id: str | None = None,
        displayed_lot_reference: str | None = None,
        listing_code_attribute: str | None = None,
    ) -> bool:
        """Store a meaningful auction state change for one auction lot."""
        car_id = self._validate_identifier(source_car_id, "source_car_id")
        if remaining_seconds is not None and (
            isinstance(remaining_seconds, bool)
            or not isinstance(remaining_seconds, int)
        ):
            raise TypeError("remaining_seconds must be int or None")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source cannot be empty")

        final_call_value = self._serialize_bool(final_call_active)
        live_bidding_value = self._serialize_bool(live_bidding)
        timestamp = self._serialize_datetime(observed_at)
        new_state = (
            auction_phase,
            bid_status,
            final_call_value,
            live_bidding_value,
            auction_end_at,
        )
        if all(value is None for value in new_state):
            return False

        with self._connect() as connection:
            vehicle_id = self._ensure_vehicle(connection, car_id, timestamp)
            auction_id = None
            if exleasingcar_auction_id is not None:
                auction_external_id = self._validate_identifier(
                    exleasingcar_auction_id, "exleasingcar_auction_id"
                )
                auction_id = self._ensure_auction(
                    connection, auction_external_id, timestamp
                )
            lot_id = self._ensure_auction_lot(
                connection,
                vehicle_id,
                auction_id,
                timestamp,
                displayed_lot_reference=displayed_lot_reference,
                listing_code_attribute=listing_code_attribute,
            )

            latest = connection.execute(
                """
                SELECT auction_phase, bid_status, final_call_active,
                       live_bidding, auction_end_at
                FROM auction_events
                WHERE auction_lot_id = ? ORDER BY id DESC LIMIT 1
                """,
                (lot_id,),
            ).fetchone()
            if latest is not None and tuple(latest) == new_state:
                return False

            connection.execute(
                """
                INSERT INTO auction_events (
                    auction_lot_id, auction_phase, bid_status,
                    final_call_active, live_bidding, auction_end_at,
                    remaining_seconds, observed_at, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lot_id,
                    auction_phase,
                    bid_status,
                    final_call_value,
                    live_bidding_value,
                    auction_end_at,
                    remaining_seconds,
                    timestamp,
                    source,
                ),
            )
        return True
