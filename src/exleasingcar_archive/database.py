import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .models import AuctionCardMarker, PriceType, SaleType
from .parsers import PriceObservation

SCHEMA_VERSION = 7
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
    displayed_title TEXT,
    chassis_number TEXT,
    chassis_number_fragment TEXT,
    chassis_number_masked INTEGER,
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
    mileage_km INTEGER,
    sale_type TEXT,
    last_detail_checked_at TEXT,
    last_detail_attempted_at TEXT,
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


PRICE_REFERENCE_OBSERVATIONS_SQL = """
CREATE TABLE IF NOT EXISTS price_reference_observations (
    id INTEGER PRIMARY KEY,
    auction_lot_id INTEGER NOT NULL,
    minimum_price TEXT,
    indicative_price TEXT,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    CHECK (minimum_price IS NOT NULL OR indicative_price IS NOT NULL),
    FOREIGN KEY (auction_lot_id) REFERENCES auction_lots(id) ON DELETE RESTRICT
)
"""


LOT_PRICE_OBSERVATIONS_SQL = """
CREATE TABLE IF NOT EXISTS lot_price_observations (
    id INTEGER PRIMARY KEY,
    auction_lot_id INTEGER NOT NULL,
    price_type TEXT NOT NULL CHECK (
        price_type IN ('displayed_auction_price', 'fixed_price')
    ),
    amount TEXT NOT NULL,
    currency TEXT,
    raw_text TEXT NOT NULL,
    source_field TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    FOREIGN KEY (auction_lot_id) REFERENCES auction_lots(id) ON DELETE RESTRICT
)
"""



AUCTION_MARKERS_SQL = """
CREATE TABLE IF NOT EXISTS auction_markers (
    id INTEGER PRIMARY KEY,
    auction_id INTEGER NOT NULL,
    icon_filename TEXT NOT NULL,
    marker_group TEXT NOT NULL,
    label TEXT,
    is_present INTEGER NOT NULL DEFAULT 1 CHECK (is_present IN (0, 1)),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    FOREIGN KEY (auction_id) REFERENCES auctions(id) ON DELETE RESTRICT,
    UNIQUE (auction_id, icon_filename)
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
        """Create schema v6 or safely migrate older database versions."""
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
                self._migrate_v2_to_v3(connection)
                self._migrate_v3_to_v4(connection)
                self._migrate_v4_to_v5(connection)
                self._migrate_v5_to_v6(connection)
                self._migrate_v6_to_v7(connection)
                self._create_indexes(connection)
            elif version == 2:
                self._migrate_v2_to_v3(connection)
                self._migrate_v3_to_v4(connection)
                self._migrate_v4_to_v5(connection)
                self._migrate_v5_to_v6(connection)
                self._migrate_v6_to_v7(connection)
                self._create_indexes(connection)
            elif version == 3:
                self._migrate_v3_to_v4(connection)
                self._migrate_v4_to_v5(connection)
                self._migrate_v5_to_v6(connection)
                self._migrate_v6_to_v7(connection)
                self._create_indexes(connection)
            elif version == 4:
                self._migrate_v4_to_v5(connection)
                self._migrate_v5_to_v6(connection)
                self._migrate_v6_to_v7(connection)
                self._create_indexes(connection)
            elif version == 5:
                self._migrate_v5_to_v6(connection)
                self._migrate_v6_to_v7(connection)
                self._create_indexes(connection)
            elif version == 6:
                self._migrate_v6_to_v7(connection)
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
            PRICE_REFERENCE_OBSERVATIONS_SQL,
            LOT_PRICE_OBSERVATIONS_SQL,
            AUCTION_MARKERS_SQL,
        ):
            connection.execute(statement)
        ArchiveDatabase._create_indexes(connection)

    @staticmethod
    def _create_indexes(connection: sqlite3.Connection) -> None:
        statements = (
            """
            CREATE INDEX IF NOT EXISTS idx_auction_lots_detail_check
            ON auction_lots(auction_id, last_detail_checked_at, id)
            """,
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
            """
            CREATE INDEX IF NOT EXISTS idx_vehicles_chassis_number
            ON vehicles(chassis_number)
            WHERE chassis_number IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_price_reference_lot_time
            ON price_reference_observations(auction_lot_id, observed_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_lot_price_type_time
            ON lot_price_observations(auction_lot_id, price_type, observed_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_auction_markers_auction_present
            ON auction_markers(auction_id, is_present, marker_group)
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

    @classmethod
    def _migrate_v2_to_v3(cls, connection: sqlite3.Connection) -> None:
        """Add the original combined vehicle title without losing stored rows."""
        cls._add_column_if_missing(
            connection,
            "vehicles",
            "displayed_title",
            "TEXT",
        )

    @classmethod
    def _migrate_v3_to_v4(cls, connection: sqlite3.Connection) -> None:
        """Add vehicle identity hints, lot mileage, and seller price references."""
        cls._add_column_if_missing(connection, "vehicles", "chassis_number", "TEXT")
        cls._add_column_if_missing(
            connection, "vehicles", "chassis_number_fragment", "TEXT"
        )
        cls._add_column_if_missing(
            connection, "vehicles", "chassis_number_masked", "INTEGER"
        )
        cls._add_column_if_missing(connection, "auction_lots", "mileage_km", "INTEGER")
        connection.execute(PRICE_REFERENCE_OBSERVATIONS_SQL)

    @classmethod
    def _migrate_v4_to_v5(cls, connection: sqlite3.Connection) -> None:
        """Persist per-lot sale mode and displayed/fixed price observations."""
        cls._add_column_if_missing(connection, "auction_lots", "sale_type", "TEXT")
        connection.execute(LOT_PRICE_OBSERVATIONS_SQL)

    @classmethod
    def _migrate_v5_to_v6(cls, connection: sqlite3.Connection) -> None:
        """Store the independent icons and labels shown on each auction card."""
        connection.execute(AUCTION_MARKERS_SQL)

    @classmethod
    def _migrate_v6_to_v7(cls, connection: sqlite3.Connection) -> None:
        """Track successful detail-page checks independently from price changes."""
        cls._add_column_if_missing(
            connection, "auction_lots", "last_detail_checked_at", "TEXT"
        )
        cls._add_column_if_missing(
            connection, "auction_lots", "last_detail_attempted_at", "TEXT"
        )

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
            raise TypeError("price must be Decimal")
        if not value.is_finite():
            raise ValueError("price must be finite")
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
        displayed_title: str | None = None,
        chassis_number: str | None = None,
        chassis_number_fragment: str | None = None,
        chassis_number_masked: bool | None = None,
        location_text: str | None = None,
        location_country_code: str | None = None,
        location_country_name: str | None = None,
        last_registration_country_code: str | None = None,
        last_registration_country_name: str | None = None,
    ) -> int:
        masked_value = ArchiveDatabase._serialize_bool(chassis_number_masked)
        if chassis_number is not None:
            if masked_value == 1 or re.search(r"[*•…]|\.{2,}", chassis_number):
                raise ValueError("A masked chassis number cannot be stored as complete")
            chassis_number = re.sub(r"[^A-Z0-9]", "", chassis_number.upper())
            if not chassis_number:
                raise ValueError("chassis_number cannot be empty after normalization")

        existing = connection.execute(
            "SELECT id, chassis_number FROM vehicles WHERE source_car_id = ?",
            (source_car_id,),
        ).fetchone()
        if existing is not None:
            existing_chassis = existing["chassis_number"]
            if (
                chassis_number is not None
                and existing_chassis is not None
                and existing_chassis != chassis_number
            ):
                raise ValueError(
                    f"Conflicting chassis number for vehicle {source_car_id}"
                )

        connection.execute(
            """
            INSERT INTO vehicles (
                source_car_id, make, model, displayed_title, chassis_number,
                chassis_number_fragment, chassis_number_masked, location_text,
                location_country_code, location_country_name,
                last_registration_country_code, last_registration_country_name,
                first_seen_at, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_car_id) DO UPDATE SET
                first_seen_at = MIN(vehicles.first_seen_at, excluded.first_seen_at),
                last_seen_at = MAX(vehicles.last_seen_at, excluded.last_seen_at),
                make = COALESCE(excluded.make, vehicles.make),
                model = COALESCE(excluded.model, vehicles.model),
                displayed_title = COALESCE(
                    excluded.displayed_title, vehicles.displayed_title
                ),
                chassis_number = COALESCE(
                    excluded.chassis_number, vehicles.chassis_number
                ),
                chassis_number_fragment = COALESCE(
                    excluded.chassis_number_fragment, vehicles.chassis_number_fragment
                ),
                chassis_number_masked = COALESCE(
                    excluded.chassis_number_masked, vehicles.chassis_number_masked
                ),
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
                displayed_title,
                chassis_number,
                chassis_number_fragment,
                masked_value,
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
        mileage_km: int | None = None,
        sale_type: str | None = None,
    ) -> int:
        if mileage_km is not None and (
            isinstance(mileage_km, bool)
            or not isinstance(mileage_km, int)
            or mileage_km < 0
        ):
            raise ValueError("mileage_km must be a non-negative integer or None")
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
                        listing_code_attribute = COALESCE(?, listing_code_attribute),
                        mileage_km = COALESCE(?, mileage_km),
                        sale_type = COALESCE(?, sale_type)
                    WHERE id = ?
                    """,
                    (
                        observed_at,
                        observed_at,
                        displayed_lot_reference,
                        listing_code_attribute,
                        mileage_km,
                        sale_type,
                        lot_id,
                    ),
                )
                return lot_id
            connection.execute(
                """
                INSERT INTO auction_lots (
                    vehicle_id, auction_id, displayed_lot_reference,
                    listing_code_attribute, mileage_km, sale_type,
                    first_seen_at, last_seen_at
                ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    vehicle_id,
                    displayed_lot_reference,
                    listing_code_attribute,
                    mileage_km,
                    sale_type,
                    observed_at,
                    observed_at,
                ),
            )
        else:
            connection.execute(
                """
                INSERT INTO auction_lots (
                    vehicle_id, auction_id, displayed_lot_reference,
                    listing_code_attribute, mileage_km, sale_type,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                    ),
                    mileage_km = COALESCE(
                        excluded.mileage_km, auction_lots.mileage_km
                    ),
                    sale_type = COALESCE(excluded.sale_type, auction_lots.sale_type)
                """,
                (
                    vehicle_id,
                    auction_id,
                    displayed_lot_reference,
                    listing_code_attribute,
                    mileage_km,
                    sale_type,
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

    @staticmethod
    def _sync_auction_markers(
        connection: sqlite3.Connection,
        auction_id: int,
        markers: tuple[AuctionCardMarker, ...],
        observed_at: str,
    ) -> None:
        """Replace the current marker set while retaining first/last seen times."""
        connection.execute(
            "UPDATE auction_markers SET is_present = 0 "
            "WHERE auction_id = ? AND is_present = 1",
            (auction_id,),
        )

        seen_icons: set[str] = set()
        for marker in markers:
            if not isinstance(marker, AuctionCardMarker):
                raise TypeError("markers must contain AuctionCardMarker values")
            icon_filename = ArchiveDatabase._validate_identifier(
                marker.icon_filename, "marker.icon_filename"
            )
            marker_group = ArchiveDatabase._validate_identifier(
                marker.marker_group, "marker.marker_group"
            )
            if marker.label is not None and not isinstance(marker.label, str):
                raise TypeError("marker.label must be str or None")
            if icon_filename in seen_icons:
                continue
            seen_icons.add(icon_filename)

            connection.execute(
                """
                INSERT INTO auction_markers (
                    auction_id, icon_filename, marker_group, label,
                    is_present, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(auction_id, icon_filename) DO UPDATE SET
                    marker_group = excluded.marker_group,
                    label = COALESCE(excluded.label, auction_markers.label),
                    is_present = 1,
                    last_seen_at = MAX(
                        auction_markers.last_seen_at, excluded.last_seen_at
                    )
                """,
                (
                    auction_id,
                    icon_filename,
                    marker_group,
                    marker.label,
                    observed_at,
                    observed_at,
                ),
            )

    def upsert_auction(
        self,
        exleasingcar_auction_id: str,
        *,
        source_auction_name: str | None = None,
        source_auction_country_code: str | None = None,
        source_auction_country_name: str | None = None,
        auction_type_label: str | None = None,
        auction_status_label: str | None = None,
        markers: tuple[AuctionCardMarker, ...] | None = None,
        observed_at: datetime | None = None,
    ) -> int:
        auction_id = self._validate_identifier(
            exleasingcar_auction_id, "exleasingcar_auction_id"
        )
        if markers is not None and not isinstance(markers, tuple):
            raise TypeError("markers must be a tuple of AuctionCardMarker or None")
        timestamp = self._serialize_datetime(observed_at)
        with self._connect() as connection:
            database_auction_id = self._ensure_auction(
                connection,
                auction_id,
                timestamp,
                source_auction_name=source_auction_name,
                source_auction_country_code=source_auction_country_code,
                source_auction_country_name=source_auction_country_name,
                auction_type_label=auction_type_label,
                auction_status_label=auction_status_label,
            )
            if markers is not None:
                self._sync_auction_markers(
                    connection,
                    database_auction_id,
                    markers,
                    timestamp,
                )
            return database_auction_id

    def upsert_vehicle(
        self,
        source_car_id: str,
        *,
        make: str | None = None,
        model: str | None = None,
        displayed_title: str | None = None,
        chassis_number: str | None = None,
        chassis_number_fragment: str | None = None,
        chassis_number_masked: bool | None = None,
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
                displayed_title=displayed_title,
                chassis_number=chassis_number,
                chassis_number_fragment=chassis_number_fragment,
                chassis_number_masked=chassis_number_masked,
                location_text=location_text,
                location_country_code=location_country_code,
                location_country_name=location_country_name,
                last_registration_country_code=last_registration_country_code,
                last_registration_country_name=last_registration_country_name,
            )

    def get_detail_check_order(
        self,
        exleasingcar_auction_id: str,
        source_car_ids: set[str],
    ) -> list[str]:
        """Order known catalogue cars by oldest successful detail-page check."""
        auction_id = self._validate_identifier(
            exleasingcar_auction_id, "exleasingcar_auction_id"
        )
        car_ids = sorted(
            {self._validate_identifier(car_id, "source_car_id") for car_id in source_car_ids}
        )
        if not car_ids:
            return []
        placeholders = ", ".join("?" for _ in car_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT v.source_car_id
                FROM auction_lots AS l
                JOIN vehicles AS v ON v.id = l.vehicle_id
                JOIN auctions AS a ON a.id = l.auction_id
                WHERE a.exleasingcar_auction_id = ?
                  AND v.source_car_id IN ({placeholders})
                ORDER BY
                    CASE WHEN MAX(
                        COALESCE(l.last_detail_checked_at, ''),
                        COALESCE(l.last_detail_attempted_at, '')
                    ) = '' THEN 0 ELSE 1 END,
                    MAX(
                        COALESCE(l.last_detail_checked_at, ''),
                        COALESCE(l.last_detail_attempted_at, '')
                    ),
                    l.id
                """,
                (auction_id, *car_ids),
            ).fetchall()
        ordered = [str(row["source_car_id"]) for row in rows]
        missing = sorted(set(car_ids) - set(ordered))
        return ordered + missing

    def mark_detail_attempted(
        self,
        source_car_id: str,
        exleasingcar_auction_id: str,
        attempted_at: datetime | None = None,
    ) -> bool:
        """Record a detail-page attempt, including failures, to prevent starvation."""
        car_id = self._validate_identifier(source_car_id, "source_car_id")
        auction_id = self._validate_identifier(
            exleasingcar_auction_id, "exleasingcar_auction_id"
        )
        timestamp = self._serialize_datetime(attempted_at)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE auction_lots
                SET last_detail_attempted_at = ?
                WHERE vehicle_id = (
                    SELECT id FROM vehicles WHERE source_car_id = ?
                )
                AND auction_id = (
                    SELECT id FROM auctions WHERE exleasingcar_auction_id = ?
                )
                """,
                (timestamp, car_id, auction_id),
            )
            return cursor.rowcount > 0

    def mark_detail_checked(
        self,
        source_car_id: str,
        exleasingcar_auction_id: str,
        checked_at: datetime | None = None,
    ) -> bool:
        """Record a successfully loaded detail page without creating price history."""
        car_id = self._validate_identifier(source_car_id, "source_car_id")
        auction_id = self._validate_identifier(
            exleasingcar_auction_id, "exleasingcar_auction_id"
        )
        timestamp = self._serialize_datetime(checked_at)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE auction_lots
                SET last_detail_checked_at = ?
                WHERE vehicle_id = (
                    SELECT id FROM vehicles WHERE source_car_id = ?
                )
                AND auction_id = (
                    SELECT id FROM auctions WHERE exleasingcar_auction_id = ?
                )
                """,
                (timestamp, car_id, auction_id),
            )
            return cursor.rowcount > 0

    def upsert_auction_lot(
        self,
        source_car_id: str,
        exleasingcar_auction_id: str | None = None,
        *,
        displayed_lot_reference: str | None = None,
        listing_code_attribute: str | None = None,
        mileage_km: int | None = None,
        sale_type: SaleType | str | None = None,
        observed_at: datetime | None = None,
    ) -> int:
        car_id = self._validate_identifier(source_car_id, "source_car_id")
        auction_external_id = None
        if exleasingcar_auction_id is not None:
            auction_external_id = self._validate_identifier(
                exleasingcar_auction_id, "exleasingcar_auction_id"
            )
        if sale_type is not None:
            try:
                normalized_sale_type = SaleType(sale_type).value
            except ValueError as exc:
                raise ValueError(f"Unsupported sale_type: {sale_type!r}") from exc
            if normalized_sale_type not in {
                SaleType.OPEN_AUCTION.value,
                SaleType.CLOSED_AUCTION.value,
                SaleType.FIXED_PRICE.value,
            }:
                raise ValueError(f"Unsupported sale_type: {sale_type!r}")
        else:
            normalized_sale_type = None
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
                mileage_km=mileage_km,
                sale_type=normalized_sale_type,
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

    def record_price_reference_observation(
        self,
        source_car_id: str,
        *,
        minimum_price: Decimal | None = None,
        indicative_price: Decimal | None = None,
        observed_at: datetime | None = None,
        source: str = DEFAULT_SOURCE,
        exleasingcar_auction_id: str | None = None,
        displayed_lot_reference: str | None = None,
        listing_code_attribute: str | None = None,
    ) -> bool:
        """Store seller reference prices separately from actual auction bids."""
        if minimum_price is None and indicative_price is None:
            return False
        minimum_value = (
            self._serialize_decimal(minimum_price) if minimum_price is not None else None
        )
        indicative_value = (
            self._serialize_decimal(indicative_price)
            if indicative_price is not None
            else None
        )
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source cannot be empty")
        car_id = self._validate_identifier(source_car_id, "source_car_id")
        timestamp = self._serialize_datetime(observed_at)

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
                SELECT minimum_price, indicative_price
                FROM price_reference_observations
                WHERE auction_lot_id = ? ORDER BY id DESC LIMIT 1
                """,
                (lot_id,),
            ).fetchone()
            new_values = (minimum_value, indicative_value)
            if latest is not None and tuple(latest) == new_values:
                return False
            connection.execute(
                """
                INSERT INTO price_reference_observations (
                    auction_lot_id, minimum_price, indicative_price,
                    observed_at, source
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (lot_id, minimum_value, indicative_value, timestamp, source),
            )
        return True

    def record_lot_price_observation(
        self,
        source_car_id: str,
        price: PriceObservation,
        observed_at: datetime | None = None,
        source: str = "vehicle_detail_html",
        *,
        exleasingcar_auction_id: str | None = None,
        displayed_lot_reference: str | None = None,
        listing_code_attribute: str | None = None,
        sale_type: SaleType | str | None = None,
    ) -> bool:
        """Store a public displayed/fixed price separately from bid history."""
        if not isinstance(price, PriceObservation):
            raise TypeError("price must be a PriceObservation")
        if price.price_type not in {
            PriceType.DISPLAYED_AUCTION_PRICE,
            PriceType.FIXED_PRICE,
        }:
            raise ValueError(
                "Only displayed auction and fixed prices may be stored here"
            )
        amount = self._serialize_decimal(price.amount)
        if not isinstance(price.raw_text, str) or not price.raw_text.strip():
            raise ValueError("price.raw_text cannot be empty")
        if not isinstance(price.source_field, str) or not price.source_field.strip():
            raise ValueError("price.source_field cannot be empty")
        if price.currency is not None and not isinstance(price.currency, str):
            raise TypeError("price.currency must be str or None")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source cannot be empty")

        car_id = self._validate_identifier(source_car_id, "source_car_id")
        auction_external_id = None
        if exleasingcar_auction_id is not None:
            auction_external_id = self._validate_identifier(
                exleasingcar_auction_id, "exleasingcar_auction_id"
            )
        if sale_type is not None:
            try:
                normalized_sale_type = SaleType(sale_type).value
            except ValueError as exc:
                raise ValueError(f"Unsupported sale_type: {sale_type!r}") from exc
            if normalized_sale_type not in {
                SaleType.OPEN_AUCTION.value,
                SaleType.CLOSED_AUCTION.value,
                SaleType.FIXED_PRICE.value,
            }:
                raise ValueError(f"Unsupported sale_type: {sale_type!r}")
        else:
            normalized_sale_type = None

        timestamp = self._serialize_datetime(observed_at)
        with self._connect() as connection:
            vehicle_id = self._ensure_vehicle(connection, car_id, timestamp)
            auction_id = (
                self._ensure_auction(connection, auction_external_id, timestamp)
                if auction_external_id is not None
                else None
            )
            lot_id = self._ensure_auction_lot(
                connection,
                vehicle_id,
                auction_id,
                timestamp,
                displayed_lot_reference=displayed_lot_reference,
                listing_code_attribute=listing_code_attribute,
                sale_type=normalized_sale_type,
            )
            latest = connection.execute(
                """
                SELECT amount, currency, source_field
                FROM lot_price_observations
                WHERE auction_lot_id = ? AND price_type = ?
                ORDER BY id DESC LIMIT 1
                """,
                (lot_id, price.price_type.value),
            ).fetchone()
            new_values = (amount, price.currency, price.source_field)
            if latest is not None and tuple(latest) == new_values:
                return False

            connection.execute(
                """
                INSERT INTO lot_price_observations (
                    auction_lot_id, price_type, amount, currency,
                    raw_text, source_field, observed_at, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lot_id,
                    price.price_type.value,
                    amount,
                    price.currency,
                    price.raw_text.strip(),
                    price.source_field.strip(),
                    timestamp,
                    source,
                ),
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
