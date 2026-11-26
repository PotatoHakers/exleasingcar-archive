import json
import math
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .models import AuctionCardMarker, PriceType, SaleType
from .parsers import PriceObservation
from .pdf_damage import PDFDamageObservation, PDFDamagePhotoReference
from .pdf_fields import PDFExtractedField, PDFFieldProvenance, PDFFieldState

SCHEMA_VERSION = 19
DEFAULT_SOURCE = "ajax_update_car_form.v3.1.php"


@dataclass(frozen=True, slots=True)
class CatalogScanProgress:
    """Persisted position of one auction catalogue scan."""

    scan_id: int
    exleasingcar_auction_id: str
    scan_type: str
    status: str
    initial_page_url: str
    next_page_url: str | None
    next_page_number: int | None
    pages_completed: int
    started_at: str
    updated_at: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class CatalogScanPage:
    """One completed catalogue page with its recoverable parsed data and API queue."""

    page_id: int
    scan_id: int
    page_number: int
    page_url: str
    item_count: int
    completed_at: str
    page_data: Mapping[str, Any]
    api_responses: tuple[Mapping[str, Any], ...]
    api_responses_processed: int


@dataclass(frozen=True, slots=True)
class AuctionDiscoveryProgress:
    """Persisted position of the global auction-list discovery scan."""

    scan_id: int
    status: str
    initial_page_url: str
    next_page_url: str | None
    next_page_number: int | None
    pages_completed: int
    started_at: str
    updated_at: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class AuctionDiscoveryPage:
    """One successfully parsed auction-list page."""

    page_id: int
    scan_id: int
    page_number: int
    page_url: str
    next_page_url: str | None
    next_page_number: int | None
    discovered_count: int
    completed_at: str


@dataclass(frozen=True, slots=True)
class AuctionBootstrapWorkItem:
    """One persisted auction waiting for its initial complete catalogue scan."""

    exleasingcar_auction_id: str
    priority: int
    status: str
    attempt_count: int
    listing_data: Mapping[str, Any]
    last_error: str | None


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
    collection_eligible INTEGER NOT NULL DEFAULT 1 CHECK (collection_eligible IN (0, 1)),
    has_fixed_price INTEGER NOT NULL DEFAULT 0 CHECK (has_fixed_price IN (0, 1)),
    monitoring_completed_at TEXT,
    monitoring_completion_reason TEXT,
    last_detail_checked_at TEXT,
    last_detail_attempted_at TEXT,
    last_api_checked_at TEXT,
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
    currency TEXT NOT NULL DEFAULT 'EUR',
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



CATALOG_SCAN_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS catalog_scan_runs (
    id INTEGER PRIMARY KEY,
    auction_id INTEGER NOT NULL,
    scan_type TEXT NOT NULL CHECK (
        scan_type IN ('bootstrap', 'routine', 'prestart')
    ),
    status TEXT NOT NULL DEFAULT 'in_progress' CHECK (
        status IN ('in_progress', 'completed')
    ),
    initial_page_url TEXT NOT NULL,
    next_page_url TEXT,
    next_page_number INTEGER,
    pages_completed INTEGER NOT NULL DEFAULT 0 CHECK (pages_completed >= 0),
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    CHECK (
        (next_page_url IS NULL AND next_page_number IS NULL)
        OR (next_page_url IS NOT NULL AND next_page_number > 0)
    ),
    CHECK (
        (status = 'in_progress' AND completed_at IS NULL)
        OR (status = 'completed' AND completed_at IS NOT NULL
            AND next_page_url IS NULL AND next_page_number IS NULL)
    ),
    FOREIGN KEY (auction_id) REFERENCES auctions(id) ON DELETE RESTRICT
)
"""


CATALOG_SCAN_PAGES_SQL = """
CREATE TABLE IF NOT EXISTS catalog_scan_pages (
    id INTEGER PRIMARY KEY,
    scan_run_id INTEGER NOT NULL,
    page_number INTEGER NOT NULL CHECK (page_number > 0),
    page_url TEXT NOT NULL,
    item_count INTEGER NOT NULL CHECK (item_count >= 0),
    completed_at TEXT NOT NULL,
    page_data_json TEXT NOT NULL DEFAULT '{}',
    api_responses_json TEXT NOT NULL DEFAULT '[]',
    api_responses_processed INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (scan_run_id) REFERENCES catalog_scan_runs(id) ON DELETE RESTRICT,
    UNIQUE (scan_run_id, page_number),
    UNIQUE (scan_run_id, page_url)
)
"""



CATALOG_DEFERRED_API_LOTS_SQL = """
CREATE TABLE IF NOT EXISTS catalog_deferred_api_lots (
    id INTEGER PRIMARY KEY,
    page_id INTEGER NOT NULL,
    source_car_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    response_payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    processed_at TEXT,
    FOREIGN KEY (page_id) REFERENCES catalog_scan_pages(id) ON DELETE RESTRICT,
    UNIQUE (page_id, source_car_id, source_url, response_payload_json, observed_at)
)
"""


AUCTION_DISCOVERY_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS auction_discovery_runs (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'in_progress' CHECK (
        status IN ('in_progress', 'completed')
    ),
    initial_page_url TEXT NOT NULL,
    next_page_url TEXT,
    next_page_number INTEGER,
    pages_completed INTEGER NOT NULL DEFAULT 0 CHECK (pages_completed >= 0),
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    CHECK (
        (next_page_url IS NULL AND next_page_number IS NULL)
        OR (next_page_url IS NOT NULL AND next_page_number > 0)
    ),
    CHECK (
        (status = 'in_progress' AND completed_at IS NULL)
        OR (status = 'completed' AND completed_at IS NOT NULL
            AND next_page_url IS NULL AND next_page_number IS NULL)
    )
)
"""


AUCTION_DISCOVERY_PAGES_SQL = """
CREATE TABLE IF NOT EXISTS auction_discovery_pages (
    id INTEGER PRIMARY KEY,
    discovery_run_id INTEGER NOT NULL,
    page_number INTEGER NOT NULL CHECK (page_number > 0),
    page_url TEXT NOT NULL,
    next_page_url TEXT,
    next_page_number INTEGER,
    discovered_count INTEGER NOT NULL CHECK (discovered_count >= 0),
    completed_at TEXT NOT NULL,
    FOREIGN KEY (discovery_run_id) REFERENCES auction_discovery_runs(id) ON DELETE RESTRICT,
    UNIQUE (discovery_run_id, page_number),
    UNIQUE (discovery_run_id, page_url)
)
"""


AUCTION_BOOTSTRAP_QUEUE_SQL = """
CREATE TABLE IF NOT EXISTS auction_bootstrap_queue (
    auction_id INTEGER PRIMARY KEY,
    source_url TEXT NOT NULL,
    listing_json TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 6,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'in_progress', 'completed')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_started_at TEXT,
    completed_at TEXT,
    last_error TEXT,
    FOREIGN KEY (auction_id) REFERENCES auctions(id) ON DELETE RESTRICT
)
"""


PRICE_REFERENCE_OBSERVATIONS_SQL = """
CREATE TABLE IF NOT EXISTS price_reference_observations (
    id INTEGER PRIMARY KEY,
    auction_lot_id INTEGER NOT NULL,
    minimum_price TEXT,
    indicative_price TEXT,
    currency TEXT NOT NULL DEFAULT 'EUR',
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
    currency TEXT DEFAULT 'EUR',
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


MEDIA_ASSETS_SQL = """
CREATE TABLE IF NOT EXISTS media_assets (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    object_key TEXT NOT NULL UNIQUE,
    mime_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    original_filename TEXT,
    created_at TEXT NOT NULL
)
"""

LOT_MEDIA_SOURCES_SQL = """
CREATE TABLE IF NOT EXISTS lot_media_sources (
    id INTEGER PRIMARY KEY,
    auction_lot_id INTEGER NOT NULL,
    media_type TEXT NOT NULL CHECK (media_type IN ('photo', 'report')),
    source_url TEXT NOT NULL,
    preview_url TEXT,
    label TEXT,
    source_order INTEGER NOT NULL DEFAULT 0 CHECK (source_order >= 0),
    download_status TEXT NOT NULL DEFAULT 'discovered' CHECK (
        download_status IN ('discovered', 'downloaded', 'failed')
    ),
    current_asset_id INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_attempted_at TEXT,
    last_downloaded_at TEXT,
    last_error TEXT,
    FOREIGN KEY (auction_lot_id) REFERENCES auction_lots(id) ON DELETE RESTRICT,
    FOREIGN KEY (current_asset_id) REFERENCES media_assets(id) ON DELETE RESTRICT,
    UNIQUE (auction_lot_id, media_type, source_url)
)
"""

MEDIA_ASSET_VERSIONS_SQL = """
CREATE TABLE IF NOT EXISTS media_asset_versions (
    id INTEGER PRIMARY KEY,
    lot_media_source_id INTEGER NOT NULL,
    media_asset_id INTEGER NOT NULL,
    linked_at TEXT NOT NULL,
    FOREIGN KEY (lot_media_source_id)
        REFERENCES lot_media_sources(id) ON DELETE RESTRICT,
    FOREIGN KEY (media_asset_id) REFERENCES media_assets(id) ON DELETE RESTRICT,
    UNIQUE (lot_media_source_id, media_asset_id)
)
"""

MEDIA_DERIVATIVES_SQL = """
CREATE TABLE IF NOT EXISTS media_derivatives (
    id INTEGER PRIMARY KEY,
    source_asset_id INTEGER NOT NULL,
    variant TEXT NOT NULL CHECK (variant IN ('card', 'gallery')),
    transform_version TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ready', 'unsupported', 'failed')),
    derived_asset_id INTEGER,
    width INTEGER,
    height INTEGER,
    last_attempted_at TEXT NOT NULL,
    last_error TEXT,
    FOREIGN KEY (source_asset_id) REFERENCES media_assets(id) ON DELETE RESTRICT,
    FOREIGN KEY (derived_asset_id) REFERENCES media_assets(id) ON DELETE RESTRICT,
    UNIQUE (source_asset_id, variant, transform_version, settings_json),
    CHECK (
        (status = 'ready' AND derived_asset_id IS NOT NULL
         AND width > 0 AND height > 0 AND last_error IS NULL)
        OR
        (status IN ('unsupported', 'failed') AND derived_asset_id IS NULL
         AND width IS NULL AND height IS NULL AND last_error IS NOT NULL)
    )
)
"""



PDF_FIELD_OBSERVATIONS_SQL = """
CREATE TABLE IF NOT EXISTS pdf_field_observations (
    id INTEGER PRIMARY KEY,
    report_sha256 TEXT NOT NULL CHECK (
        length(report_sha256) = 64 AND report_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    source_lot_id TEXT,
    auction_lot_id INTEGER,
    page_number INTEGER NOT NULL CHECK (page_number > 0),
    field_key TEXT NOT NULL CHECK (length(trim(field_key)) > 0),
    source_label TEXT NOT NULL CHECK (length(trim(source_label)) > 0),
    raw_text TEXT NOT NULL,
    normalized_text TEXT,
    state TEXT NOT NULL CHECK (
        state IN (
            'value', 'blank', 'explicit_no', 'not_applicable', 'not_tested', 'unknown'
        )
    ),
    parser_name TEXT NOT NULL CHECK (length(trim(parser_name)) > 0),
    parser_version TEXT NOT NULL CHECK (length(trim(parser_version)) > 0),
    extracted_at TEXT NOT NULL,
    FOREIGN KEY (auction_lot_id) REFERENCES auction_lots(id) ON DELETE RESTRICT
)
"""


PDF_DAMAGE_OBSERVATIONS_SQL = """
CREATE TABLE IF NOT EXISTS pdf_damage_observations (
    id INTEGER PRIMARY KEY,
    report_sha256 TEXT NOT NULL CHECK (
        length(report_sha256) = 64 AND report_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    source_lot_id TEXT,
    auction_lot_id INTEGER,
    item_number INTEGER NOT NULL CHECK (item_number > 0),
    category TEXT NOT NULL CHECK (length(trim(category)) > 0),
    component TEXT NOT NULL CHECK (length(trim(component)) > 0),
    damage_description TEXT NOT NULL CHECK (length(trim(damage_description)) > 0),
    page_number INTEGER NOT NULL CHECK (page_number > 0),
    photo_page_number INTEGER,
    photo_index INTEGER,
    photo_caption TEXT,
    photo_x0 REAL,
    photo_top REAL,
    photo_x1 REAL,
    photo_bottom REAL,
    parser_name TEXT NOT NULL CHECK (length(trim(parser_name)) > 0),
    parser_version TEXT NOT NULL CHECK (length(trim(parser_version)) > 0),
    extracted_at TEXT NOT NULL,
    CHECK (
        (photo_page_number IS NULL AND photo_index IS NULL AND photo_caption IS NULL
         AND photo_x0 IS NULL AND photo_top IS NULL AND photo_x1 IS NULL
         AND photo_bottom IS NULL)
        OR
        (photo_page_number > 0 AND photo_index > 0
         AND photo_caption IS NOT NULL AND length(trim(photo_caption)) > 0
         AND photo_x0 IS NOT NULL AND photo_top IS NOT NULL
         AND photo_x1 > photo_x0 AND photo_bottom > photo_top)
    ),
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
        """Create the current schema or safely migrate older database versions."""
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
            else:
                # Apply every migration after the database's current schema.
                # Keeping this as one ordered chain prevents older databases
                # from skipping a later migration when a new schema is added.
                migrations = (
                    (1, self._migrate_v1_to_v2),
                    (2, self._migrate_v2_to_v3),
                    (3, self._migrate_v3_to_v4),
                    (4, self._migrate_v4_to_v5),
                    (5, self._migrate_v5_to_v6),
                    (6, self._migrate_v6_to_v7),
                    (7, self._migrate_v7_to_v8),
                    (8, self._migrate_v8_to_v9),
                    (9, self._migrate_v9_to_v10),
                    (10, self._migrate_v10_to_v11),
                    (11, self._migrate_v11_to_v12),
                    (12, self._migrate_v12_to_v13),
                    (13, self._migrate_v13_to_v14),
                    (14, self._migrate_v14_to_v15),
                    (15, self._migrate_v15_to_v16),
                    (16, self._migrate_v16_to_v17),
                    (17, self._migrate_v17_to_v18),
                    (18, self._migrate_v18_to_v19),
                )
                for from_version, migration in migrations:
                    if version <= from_version:
                        migration(connection)
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
            MEDIA_ASSETS_SQL,
            LOT_MEDIA_SOURCES_SQL,
            MEDIA_ASSET_VERSIONS_SQL,
            PDF_FIELD_OBSERVATIONS_SQL,
            PDF_DAMAGE_OBSERVATIONS_SQL,
            MEDIA_DERIVATIVES_SQL,
            CATALOG_SCAN_RUNS_SQL,
            CATALOG_SCAN_PAGES_SQL,
            CATALOG_DEFERRED_API_LOTS_SQL,
            AUCTION_DISCOVERY_RUNS_SQL,
            AUCTION_DISCOVERY_PAGES_SQL,
            AUCTION_BOOTSTRAP_QUEUE_SQL,
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
            CREATE INDEX IF NOT EXISTS idx_auction_bootstrap_queue_status
            ON auction_bootstrap_queue(status, priority, last_started_at, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_auction_discovery_pages_run
            ON auction_discovery_pages(discovery_run_id, page_number)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_auction_lots_collection_eligible
            ON auction_lots(collection_eligible, auction_id, last_seen_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_auction_lots_auction_vehicle
            ON auction_lots(auction_id, vehicle_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_auction_lots_monitoring_active
            ON auction_lots(auction_id, collection_eligible, monitoring_completed_at)
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
            """
            CREATE INDEX IF NOT EXISTS idx_lot_media_sources_lot_order
            ON lot_media_sources(auction_lot_id, media_type, source_order, id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_media_asset_versions_source
            ON media_asset_versions(lot_media_source_id, linked_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_media_derivatives_source_variant
            ON media_derivatives(source_asset_id, variant, transform_version)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_pdf_field_observations_report_page
            ON pdf_field_observations(report_sha256, page_number, id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_pdf_field_observations_lot
            ON pdf_field_observations(auction_lot_id, id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_pdf_damage_observations_report
            ON pdf_damage_observations(report_sha256, page_number, item_number, id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_pdf_damage_observations_lot
            ON pdf_damage_observations(auction_lot_id, id)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_catalog_scan_one_active_per_auction
            ON catalog_scan_runs(auction_id)
            WHERE status = 'in_progress'
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_catalog_scan_pages_run
            ON catalog_scan_pages(scan_run_id, page_number)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_catalog_deferred_api_pending
            ON catalog_deferred_api_lots(source_car_id, processed_at, id)
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

    @classmethod
    def _migrate_v7_to_v8(cls, connection: sqlite3.Connection) -> None:
        """Track the latest successful API response check for each auction lot."""
        cls._add_column_if_missing(
            connection, "auction_lots", "last_api_checked_at", "TEXT"
        )

    @classmethod
    def _migrate_v8_to_v9(cls, connection: sqlite3.Connection) -> None:
        """Record EUR explicitly on every monetary observation."""
        cls._add_column_if_missing(
            connection,
            "price_observations",
            "currency",
            "TEXT NOT NULL DEFAULT 'EUR'",
        )
        cls._add_column_if_missing(
            connection,
            "price_reference_observations",
            "currency",
            "TEXT NOT NULL DEFAULT 'EUR'",
        )
        connection.execute(
            """
            UPDATE lot_price_observations
            SET currency = 'EUR'
            WHERE currency IS NULL OR TRIM(currency) = ''
            """
        )

    @classmethod
    def _migrate_v9_to_v10(cls, connection: sqlite3.Connection) -> None:
        """Create lot-scoped media source and immutable asset registries."""
        for statement in (
            MEDIA_ASSETS_SQL,
            LOT_MEDIA_SOURCES_SQL,
            MEDIA_ASSET_VERSIONS_SQL,
        ):
            connection.execute(statement)

    @classmethod
    def _migrate_v10_to_v11(cls, connection: sqlite3.Connection) -> None:
        """Add durable storage for extracted PDF field observations."""
        connection.execute(PDF_FIELD_OBSERVATIONS_SQL)

    @classmethod
    def _migrate_v11_to_v12(cls, connection: sqlite3.Connection) -> None:
        """Add separate storage for numbered PDF damage observations."""
        connection.execute(PDF_DAMAGE_OBSERVATIONS_SQL)

    @classmethod
    def _migrate_v12_to_v13(cls, connection: sqlite3.Connection) -> None:
        """Add source-linked image preview derivatives."""
        connection.execute(MEDIA_DERIVATIVES_SQL)

    @classmethod
    def _migrate_v13_to_v14(cls, connection: sqlite3.Connection) -> None:
        """Track current archive eligibility independently from auction mode."""
        cls._add_column_if_missing(
            connection,
            "auction_lots",
            "collection_eligible",
            "INTEGER NOT NULL DEFAULT 1 CHECK (collection_eligible IN (0, 1))",
        )
        cls._add_column_if_missing(
            connection,
            "auction_lots",
            "has_fixed_price",
            "INTEGER NOT NULL DEFAULT 0 CHECK (has_fixed_price IN (0, 1))",
        )
        connection.execute(
            """
            UPDATE auction_lots
            SET has_fixed_price = CASE WHEN EXISTS (
                SELECT 1
                FROM lot_price_observations AS price
                WHERE price.auction_lot_id = auction_lots.id
                  AND price.price_type = 'fixed_price'
            ) THEN 1 ELSE 0 END
            """
        )
        connection.execute(
            """
            UPDATE auction_lots
            SET collection_eligible = CASE
                WHEN sale_type = 'open_auction' THEN 1
                WHEN has_fixed_price = 1 THEN 1
                ELSE 0
            END
            """
        )

    @classmethod
    def _migrate_v18_to_v19(cls, connection: sqlite3.Connection) -> None:
        """Persist global auction discovery and resumable bootstrap work."""
        connection.execute(AUCTION_DISCOVERY_RUNS_SQL)
        connection.execute(AUCTION_DISCOVERY_PAGES_SQL)
        connection.execute(AUCTION_BOOTSTRAP_QUEUE_SQL)

    @classmethod
    def _migrate_v17_to_v18(cls, connection: sqlite3.Connection) -> None:
        """Keep unclassified public lot updates separately for later replay."""
        connection.execute(CATALOG_DEFERRED_API_LOTS_SQL)

    @classmethod
    def _migrate_v16_to_v17(cls, connection: sqlite3.Connection) -> None:
        """Store replayable parsed page data and API response queues per checkpoint."""
        cls._add_column_if_missing(
            connection,
            "catalog_scan_pages",
            "page_data_json",
            "TEXT NOT NULL DEFAULT '{}'",
        )
        cls._add_column_if_missing(
            connection,
            "catalog_scan_pages",
            "api_responses_json",
            "TEXT NOT NULL DEFAULT '[]'",
        )
        cls._add_column_if_missing(
            connection,
            "catalog_scan_pages",
            "api_responses_processed",
            "INTEGER NOT NULL DEFAULT 0",
        )

    @classmethod
    def _migrate_v15_to_v16(cls, connection: sqlite3.Connection) -> None:
        """Persist resumable catalogue scan cursors and completed page checkpoints."""
        connection.execute(CATALOG_SCAN_RUNS_SQL)
        connection.execute(CATALOG_SCAN_PAGES_SQL)

    @classmethod
    def _migrate_v14_to_v15(cls, connection: sqlite3.Connection) -> None:
        """Track conclusively completed open auctions without discarding history."""
        cls._add_column_if_missing(
            connection,
            "auction_lots",
            "monitoring_completed_at",
            "TEXT",
        )
        cls._add_column_if_missing(
            connection,
            "auction_lots",
            "monitoring_completion_reason",
            "TEXT",
        )
        connection.execute(
            """
            UPDATE auction_lots
            SET monitoring_completed_at = (
                    SELECT event.observed_at
                    FROM auction_events AS event
                    WHERE event.id = (
                        SELECT latest.id
                        FROM auction_events AS latest
                        WHERE latest.auction_lot_id = auction_lots.id
                        ORDER BY latest.observed_at DESC, latest.id DESC
                        LIMIT 1
                    )
                ),
                monitoring_completion_reason = 'countdown_expired'
            WHERE sale_type = 'open_auction'
              AND EXISTS (
                    SELECT 1
                    FROM auction_events AS event
                    WHERE event.id = (
                        SELECT latest.id
                        FROM auction_events AS latest
                        WHERE latest.auction_lot_id = auction_lots.id
                        ORDER BY latest.observed_at DESC, latest.id DESC
                        LIMIT 1
                    )
                      AND event.remaining_seconds <= 0
                      AND event.live_bidding = 0
                      AND event.final_call_active = 0
                )
            """
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
        collection_eligible: bool | None = None,
    ) -> int:
        if mileage_km is not None and (
            isinstance(mileage_km, bool)
            or not isinstance(mileage_km, int)
            or mileage_km < 0
        ):
            raise ValueError("mileage_km must be a non-negative integer or None")
        if collection_eligible is not None and not isinstance(collection_eligible, bool):
            raise TypeError("collection_eligible must be bool or None")
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
                        sale_type = COALESCE(?, sale_type),
                        collection_eligible = COALESCE(?, collection_eligible)
                    WHERE id = ?
                    """,
                    (
                        observed_at,
                        observed_at,
                        displayed_lot_reference,
                        listing_code_attribute,
                        mileage_km,
                        sale_type,
                        int(collection_eligible) if collection_eligible is not None else None,
                        lot_id,
                    ),
                )
                return lot_id
            connection.execute(
                """
                INSERT INTO auction_lots (
                    vehicle_id, auction_id, displayed_lot_reference,
                    listing_code_attribute, mileage_km, sale_type,
                    collection_eligible, first_seen_at, last_seen_at
                ) VALUES (?, NULL, ?, ?, ?, ?, COALESCE(?, 0), ?, ?)
                """,
                (
                    vehicle_id,
                    displayed_lot_reference,
                    listing_code_attribute,
                    mileage_km,
                    sale_type,
                    int(collection_eligible) if collection_eligible is not None else None,
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
                    collection_eligible, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, 0), ?, ?)
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
                    sale_type = COALESCE(excluded.sale_type, auction_lots.sale_type),
                    collection_eligible = CASE
                        WHEN ? IS NULL THEN auction_lots.collection_eligible
                        ELSE excluded.collection_eligible
                    END
                """,
                (
                    vehicle_id,
                    auction_id,
                    displayed_lot_reference,
                    listing_code_attribute,
                    mileage_km,
                    sale_type,
                    int(collection_eligible) if collection_eligible is not None else None,
                    observed_at,
                    observed_at,
                    int(collection_eligible) if collection_eligible is not None else None,
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

    @staticmethod
    def _validate_catalog_page_url(value: str, field_name: str) -> str:
        normalized = ArchiveDatabase._validate_identifier(value, field_name)
        parsed = urlsplit(normalized)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError(f"{field_name} has an invalid port") from exc
        if (
            parsed.scheme != "https"
            or not (
                hostname == "exleasingcar.com"
                or hostname.endswith(".exleasingcar.com")
            )
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
        ):
            raise ValueError(f"{field_name} must be an HTTPS Exleasingcar URL")
        return normalized

    @staticmethod
    def _catalog_scan_progress(
        connection: sqlite3.Connection,
        scan_id: int,
    ) -> CatalogScanProgress | None:
        row = connection.execute(
            """
            SELECT runs.id, auctions.exleasingcar_auction_id, runs.scan_type,
                   runs.status, runs.initial_page_url, runs.next_page_url,
                   runs.next_page_number, runs.pages_completed, runs.started_at,
                   runs.updated_at, runs.completed_at
            FROM catalog_scan_runs AS runs
            JOIN auctions ON auctions.id = runs.auction_id
            WHERE runs.id = ?
            """,
            (scan_id,),
        ).fetchone()
        if row is None:
            return None
        return CatalogScanProgress(
            scan_id=int(row["id"]),
            exleasingcar_auction_id=str(row["exleasingcar_auction_id"]),
            scan_type=str(row["scan_type"]),
            status=str(row["status"]),
            initial_page_url=str(row["initial_page_url"]),
            next_page_url=(
                str(row["next_page_url"]) if row["next_page_url"] is not None else None
            ),
            next_page_number=(
                int(row["next_page_number"])
                if row["next_page_number"] is not None
                else None
            ),
            pages_completed=int(row["pages_completed"]),
            started_at=str(row["started_at"]),
            updated_at=str(row["updated_at"]),
            completed_at=(
                str(row["completed_at"]) if row["completed_at"] is not None else None
            ),
        )

    def start_or_resume_catalog_scan(
        self,
        exleasingcar_auction_id: str,
        scan_type: str,
        first_page_url: str,
        *,
        observed_at: datetime | None = None,
    ) -> CatalogScanProgress:
        """Start a catalogue scan or return its existing unfinished checkpoint.

        Only one unfinished catalogue scan is allowed per auction. A process
        restart therefore resumes the saved cursor instead of silently
        creating a fresh generation at page one.
        """
        auction_id = self._validate_identifier(
            exleasingcar_auction_id, "exleasingcar_auction_id"
        )
        scan_kind = self._validate_identifier(scan_type, "scan_type")
        if scan_kind not in {"bootstrap", "routine", "prestart"}:
            raise ValueError("scan_type must be bootstrap, routine, or prestart")
        first_url = self._validate_catalog_page_url(first_page_url, "first_page_url")
        timestamp = self._serialize_datetime(observed_at)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            database_auction_id = self._ensure_auction(
                connection, auction_id, timestamp
            )
            active = connection.execute(
                """
                SELECT id, scan_type
                FROM catalog_scan_runs
                WHERE auction_id = ? AND status = 'in_progress'
                ORDER BY id DESC
                LIMIT 1
                """,
                (database_auction_id,),
            ).fetchone()
            if active is not None:
                if str(active["scan_type"]) != scan_kind:
                    raise RuntimeError(
                        "An unfinished catalogue scan already exists for this auction "
                        f"with scan_type={active['scan_type']!r}"
                    )
                progress = self._catalog_scan_progress(
                    connection, int(active["id"])
                )
                assert progress is not None
                return progress

            cursor = connection.execute(
                """
                INSERT INTO catalog_scan_runs (
                    auction_id, scan_type, status, initial_page_url,
                    next_page_url, next_page_number, pages_completed,
                    started_at, updated_at, completed_at
                ) VALUES (?, ?, 'in_progress', ?, ?, 1, 0, ?, ?, NULL)
                """,
                (
                    database_auction_id,
                    scan_kind,
                    first_url,
                    first_url,
                    timestamp,
                    timestamp,
                ),
            )
            progress = self._catalog_scan_progress(connection, int(cursor.lastrowid))
            assert progress is not None
            return progress

    def get_catalog_scan_progress(self, scan_id: int) -> CatalogScanProgress | None:
        """Return the current persisted cursor for one catalogue scan."""
        if isinstance(scan_id, bool) or not isinstance(scan_id, int) or scan_id < 1:
            raise ValueError("scan_id must be a positive integer")
        with self._connect() as connection:
            return self._catalog_scan_progress(connection, scan_id)

    @staticmethod
    def _public_catalog_api_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        """Keep parser-required public auction fields; drop account-specific values.

        The source response can include user_offer and actual_car_price. Those
        values are account-specific and must never be persisted in the archive's
        durable response queue.
        """
        public_car_fields = {
            "car_id",
            "highest_offer",
            "text_minimali_kaina",
            "text_orientacine_kaina",
            "bid_status",
            "aukciono_tipas",
            "liko_s",
        }
        public_time_fields = {
            "type",
            "image",
            "laikas",
            "remainingSeconds",
            "endDate",
            "liveBidding",
        }
        result: dict[str, Any] = {}
        for raw_car_id, raw_car_data in payload.items():
            car_id = str(raw_car_id)
            if raw_car_data is None:
                result[car_id] = None
                continue
            if not isinstance(raw_car_data, Mapping):
                # Preserve the key while discarding unexpected/non-object data.
                result[car_id] = None
                continue
            safe_data = {
                key: raw_car_data[key]
                for key in public_car_fields
                if key in raw_car_data
            }
            if "car_auction_time" in raw_car_data:
                raw_time = raw_car_data["car_auction_time"]
                if isinstance(raw_time, Mapping):
                    safe_data["car_auction_time"] = {
                        key: raw_time[key]
                        for key in public_time_fields
                        if key in raw_time
                    }
                else:
                    safe_data["car_auction_time"] = None
            result[car_id] = safe_data
        return result

    @classmethod
    def _normalize_catalog_api_responses(
        cls,
        api_responses: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        if not isinstance(api_responses, Sequence) or isinstance(
            api_responses, (str, bytes)
        ):
            raise TypeError("api_responses must be a sequence of mappings")
        normalized: list[dict[str, Any]] = []
        for response in api_responses:
            if not isinstance(response, Mapping):
                raise TypeError("each API response must be a mapping")
            validated_url = cls._validate_catalog_page_url(
                response.get("url"), "api_response.url"
            )
            parsed_url = urlsplit(validated_url)
            # API query strings are not needed for replay and can contain
            # account/session-specific parameters.
            response_url = urlunsplit(
                (parsed_url.scheme, parsed_url.netloc, parsed_url.path, "", "")
            )
            payload = response.get("payload")
            if not isinstance(payload, Mapping):
                raise TypeError("API response payload must be a mapping")
            observed_at = response.get("observed_at")
            if not isinstance(observed_at, datetime):
                raise TypeError("API response observed_at must be a datetime")
            normalized.append(
                {
                    "url": response_url,
                    "payload": cls._public_catalog_api_payload(payload),
                    "observed_at": cls._serialize_datetime(observed_at),
                }
            )
        try:
            json.dumps(normalized, ensure_ascii=False, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "API response payloads must contain finite JSON-compatible values"
            ) from exc
        return normalized

    def record_catalog_page_completed(
        self,
        scan_id: int,
        page_number: int,
        page_url: str,
        *,
        next_page_url: str | None,
        next_page_number: int | None,
        item_count: int,
        page_data: Mapping[str, Any] | None = None,
        api_responses: Sequence[Mapping[str, Any]] = (),
        observed_at: datetime | None = None,
    ) -> bool:
        """Checkpoint one fully processed page and advance the saved cursor.

        Call this only after every row/API observation from the page has been
        successfully persisted. A crash before the checkpoint causes the page
        to be replayed; callers must keep page ingestion idempotent.
        """
        for value, name, minimum in (
            (scan_id, "scan_id", 1),
            (page_number, "page_number", 1),
            (item_count, "item_count", 0),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < minimum:
                raise ValueError(f"{name} must be at least {minimum}")
        current_url = self._validate_catalog_page_url(page_url, "page_url")
        if next_page_url is None:
            if next_page_number is not None:
                raise ValueError("next_page_number must be None when there is no next page")
            following_url = None
        else:
            following_url = self._validate_catalog_page_url(
                next_page_url, "next_page_url"
            )
            if isinstance(next_page_number, bool) or not isinstance(next_page_number, int):
                raise TypeError("next_page_number must be an integer when next_page_url exists")
            if next_page_number <= page_number:
                raise ValueError("next_page_number must be greater than page_number")
            if following_url == current_url:
                raise ValueError("next_page_url cannot repeat the current page URL")

        if page_data is None:
            normalized_page_data: Mapping[str, Any] = {}
        elif isinstance(page_data, Mapping):
            normalized_page_data = page_data
        else:
            raise TypeError("page_data must be a mapping or None")
        try:
            page_data_json = json.dumps(
                normalized_page_data,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("page_data must contain finite JSON-compatible values") from exc

        normalized_api_responses = self._normalize_catalog_api_responses(
            api_responses
        )
        api_responses_json = json.dumps(
            normalized_api_responses,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )

        timestamp = self._serialize_datetime(observed_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            scan = connection.execute(
                """
                SELECT auction_id, status, next_page_url, next_page_number,
                       pages_completed
                FROM catalog_scan_runs
                WHERE id = ?
                """,
                (scan_id,),
            ).fetchone()
            if scan is None:
                raise KeyError(f"Unknown catalogue scan id: {scan_id}")

            existing = connection.execute(
                """
                SELECT page_number, page_url, item_count
                FROM catalog_scan_pages
                WHERE scan_run_id = ? AND (page_number = ? OR page_url = ?)
                """,
                (scan_id, page_number, current_url),
            ).fetchone()
            if existing is not None:
                if (
                    int(existing["page_number"]) == page_number
                    and str(existing["page_url"]) == current_url
                    and int(existing["item_count"]) == item_count
                ):
                    return False
                raise ValueError("Page checkpoint conflicts with an already saved page")

            if str(scan["status"]) != "in_progress":
                raise ValueError("Cannot checkpoint a page for a completed scan")
            if scan["next_page_url"] is None or scan["next_page_number"] is None:
                raise ValueError("The scan has no remaining page to checkpoint")
            if (
                int(scan["next_page_number"]) != page_number
                or str(scan["next_page_url"]) != current_url
            ):
                raise ValueError(
                    "Page does not match the saved catalogue cursor; "
                    "resume from the persisted next_page_url and next_page_number"
                )

            if following_url is not None:
                repeated = connection.execute(
                    """
                    SELECT 1 FROM catalog_scan_pages
                    WHERE scan_run_id = ? AND page_url = ?
                    """,
                    (scan_id, following_url),
                ).fetchone()
                if repeated is not None:
                    raise ValueError("next_page_url points to a page already checkpointed")

            connection.execute(
                """
                INSERT INTO catalog_scan_pages (
                    scan_run_id, page_number, page_url, item_count, completed_at,
                    page_data_json, api_responses_json, api_responses_processed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    scan_id,
                    page_number,
                    current_url,
                    item_count,
                    timestamp,
                    page_data_json,
                    api_responses_json,
                ),
            )
            pages_completed = int(scan["pages_completed"]) + 1
            connection.execute(
                """
                UPDATE catalog_scan_runs
                SET next_page_url = ?, next_page_number = ?,
                    pages_completed = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    following_url,
                    next_page_number,
                    pages_completed,
                    timestamp,
                    scan_id,
                ),
            )
            return True


    def has_completed_catalog_scan(
        self,
        exleasingcar_auction_id: str,
        scan_type: str = "bootstrap",
    ) -> bool:
        """Return whether the auction already has a completed scan of this type."""
        auction_id = self._validate_identifier(
            exleasingcar_auction_id, "exleasingcar_auction_id"
        )
        scan_kind = self._validate_identifier(scan_type, "scan_type")
        if scan_kind not in {"bootstrap", "routine", "prestart"}:
            raise ValueError("scan_type must be bootstrap, routine, or prestart")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM catalog_scan_runs AS runs
                JOIN auctions ON auctions.id = runs.auction_id
                WHERE auctions.exleasingcar_auction_id = ?
                  AND runs.scan_type = ?
                  AND runs.status = 'completed'
                LIMIT 1
                """,
                (auction_id, scan_kind),
            ).fetchone()
        return row is not None

    @staticmethod
    def _catalog_scan_page_from_row(row: sqlite3.Row) -> CatalogScanPage:
        try:
            page_data = json.loads(str(row["page_data_json"]))
            api_responses = json.loads(str(row["api_responses_json"]))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid persisted catalogue page data for page {row['page_number']}"
            ) from exc
        if not isinstance(page_data, Mapping) or not isinstance(api_responses, list):
            raise TypeError(
                f"Invalid persisted catalogue page shape for page {row['page_number']}"
            )
        if any(not isinstance(item, Mapping) for item in api_responses):
            raise RuntimeError(
                f"Invalid API response queue for page {row['page_number']}"
            )
        processed = int(row["api_responses_processed"])
        if processed < 0 or processed > len(api_responses):
            raise RuntimeError(
                f"Invalid API response cursor for page {row['page_number']}"
            )
        return CatalogScanPage(
            page_id=int(row["id"]),
            scan_id=int(row["scan_run_id"]),
            page_number=int(row["page_number"]),
            page_url=str(row["page_url"]),
            item_count=int(row["item_count"]),
            completed_at=str(row["completed_at"]),
            page_data=page_data,
            api_responses=tuple(api_responses),
            api_responses_processed=processed,
        )

    @staticmethod
    def _auction_discovery_progress(
        connection: sqlite3.Connection,
        scan_id: int,
    ) -> AuctionDiscoveryProgress | None:
        row = connection.execute(
            """
            SELECT id, status, initial_page_url, next_page_url, next_page_number,
                   pages_completed, started_at, updated_at, completed_at
            FROM auction_discovery_runs
            WHERE id = ?
            """,
            (scan_id,),
        ).fetchone()
        if row is None:
            return None
        return AuctionDiscoveryProgress(
            scan_id=int(row["id"]),
            status=str(row["status"]),
            initial_page_url=str(row["initial_page_url"]),
            next_page_url=(
                str(row["next_page_url"]) if row["next_page_url"] is not None else None
            ),
            next_page_number=(
                int(row["next_page_number"])
                if row["next_page_number"] is not None
                else None
            ),
            pages_completed=int(row["pages_completed"]),
            started_at=str(row["started_at"]),
            updated_at=str(row["updated_at"]),
            completed_at=(
                str(row["completed_at"]) if row["completed_at"] is not None else None
            ),
        )

    def start_or_resume_auction_discovery(
        self,
        first_page_url: str,
        *,
        observed_at: datetime | None = None,
        force: bool = False,
        minimum_interval_hours: int = 24,
    ) -> AuctionDiscoveryProgress:
        """Resume an unfinished global discovery scan or start one when due.

        A completed discovery is reused for 24 hours by default. Queue work
        remains independently resumable on every collector invocation.
        """
        first_url = self._validate_catalog_page_url(first_page_url, "first_page_url")
        if not isinstance(force, bool):
            raise TypeError("force must be bool")
        if (
            isinstance(minimum_interval_hours, bool)
            or not isinstance(minimum_interval_hours, int)
            or minimum_interval_hours < 1
        ):
            raise ValueError("minimum_interval_hours must be a positive integer")
        timestamp = self._serialize_datetime(observed_at)
        now = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        cutoff = self._serialize_datetime(
            now - timedelta(hours=minimum_interval_hours)
        )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT id FROM auction_discovery_runs
                WHERE status = 'in_progress'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            if active is not None:
                progress = self._auction_discovery_progress(
                    connection, int(active["id"])
                )
                assert progress is not None
                return progress

            latest = connection.execute(
                """
                SELECT id, status, completed_at
                FROM auction_discovery_runs
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            if (
                not force
                and latest is not None
                and str(latest["status"]) == "completed"
                and latest["completed_at"] is not None
                and str(latest["completed_at"]) >= cutoff
            ):
                progress = self._auction_discovery_progress(
                    connection, int(latest["id"])
                )
                assert progress is not None
                return progress

            cursor = connection.execute(
                """
                INSERT INTO auction_discovery_runs (
                    status, initial_page_url, next_page_url, next_page_number,
                    pages_completed, started_at, updated_at, completed_at
                ) VALUES ('in_progress', ?, ?, 1, 0, ?, ?, NULL)
                """,
                (first_url, first_url, timestamp, timestamp),
            )
            progress = self._auction_discovery_progress(
                connection, int(cursor.lastrowid)
            )
            assert progress is not None
            return progress

    def get_auction_discovery_pages(
        self,
        scan_id: int,
    ) -> tuple[AuctionDiscoveryPage, ...]:
        if isinstance(scan_id, bool) or not isinstance(scan_id, int) or scan_id < 1:
            raise ValueError("scan_id must be a positive integer")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, discovery_run_id, page_number, page_url, next_page_url,
                       next_page_number, discovered_count, completed_at
                FROM auction_discovery_pages
                WHERE discovery_run_id = ?
                ORDER BY page_number, id
                """,
                (scan_id,),
            ).fetchall()
        return tuple(
            AuctionDiscoveryPage(
                page_id=int(row["id"]),
                scan_id=int(row["discovery_run_id"]),
                page_number=int(row["page_number"]),
                page_url=str(row["page_url"]),
                next_page_url=(
                    str(row["next_page_url"])
                    if row["next_page_url"] is not None
                    else None
                ),
                next_page_number=(
                    int(row["next_page_number"])
                    if row["next_page_number"] is not None
                    else None
                ),
                discovered_count=int(row["discovered_count"]),
                completed_at=str(row["completed_at"]),
            )
            for row in rows
        )

    def record_auction_discovery_page_completed(
        self,
        scan_id: int,
        page_number: int,
        page_url: str,
        *,
        next_page_url: str | None,
        next_page_number: int | None,
        discovered_count: int,
        observed_at: datetime | None = None,
    ) -> bool:
        """Atomically persist a discovery page and its next-page cursor."""
        for value, name, minimum in (
            (scan_id, "scan_id", 1),
            (page_number, "page_number", 1),
            (discovered_count, "discovered_count", 0),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < minimum:
                raise ValueError(f"{name} must be at least {minimum}")
        current_url = self._validate_catalog_page_url(page_url, "page_url")
        if next_page_url is None:
            if next_page_number is not None:
                raise ValueError("next_page_number must be None when no next URL exists")
            following_url = None
        else:
            following_url = self._validate_catalog_page_url(
                next_page_url, "next_page_url"
            )
            if isinstance(next_page_number, bool) or not isinstance(next_page_number, int):
                raise TypeError("next_page_number must be an integer")
            if next_page_number != page_number + 1:
                raise ValueError("next_page_number must follow page_number")
            if following_url == current_url:
                raise ValueError("next_page_url cannot repeat the current page")

        timestamp = self._serialize_datetime(observed_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            scan = connection.execute(
                """
                SELECT status, next_page_url, next_page_number, pages_completed
                FROM auction_discovery_runs WHERE id = ?
                """,
                (scan_id,),
            ).fetchone()
            if scan is None:
                raise KeyError(f"Unknown auction discovery scan id: {scan_id}")
            if str(scan["status"]) != "in_progress":
                return False
            if (
                str(scan["next_page_url"]) != current_url
                or int(scan["next_page_number"]) != page_number
            ):
                raise RuntimeError("Auction discovery page does not match its saved cursor")
            if following_url is not None:
                repeated = connection.execute(
                    """
                    SELECT 1 FROM auction_discovery_pages
                    WHERE discovery_run_id = ? AND page_url = ?
                    """,
                    (scan_id, following_url),
                ).fetchone()
                if repeated is not None:
                    raise ValueError("Auction discovery pagination points to a visited URL")

            connection.execute(
                """
                INSERT INTO auction_discovery_pages (
                    discovery_run_id, page_number, page_url, next_page_url,
                    next_page_number, discovered_count, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_id,
                    page_number,
                    current_url,
                    following_url,
                    next_page_number,
                    discovered_count,
                    timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE auction_discovery_runs
                SET next_page_url = ?, next_page_number = ?,
                    pages_completed = pages_completed + 1, updated_at = ?
                WHERE id = ?
                """,
                (following_url, next_page_number, timestamp, scan_id),
            )
            return True

    def complete_auction_discovery(
        self,
        scan_id: int,
        *,
        observed_at: datetime | None = None,
    ) -> bool:
        if isinstance(scan_id, bool) or not isinstance(scan_id, int) or scan_id < 1:
            raise ValueError("scan_id must be a positive integer")
        timestamp = self._serialize_datetime(observed_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            scan = connection.execute(
                """
                SELECT status, next_page_url, next_page_number, pages_completed
                FROM auction_discovery_runs WHERE id = ?
                """,
                (scan_id,),
            ).fetchone()
            if scan is None:
                raise KeyError(f"Unknown auction discovery scan id: {scan_id}")
            if str(scan["status"]) == "completed":
                return False
            if scan["next_page_url"] is not None or scan["next_page_number"] is not None:
                raise ValueError("Cannot complete auction discovery while pages remain")
            page_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM auction_discovery_pages
                    WHERE discovery_run_id = ?
                    """,
                    (scan_id,),
                ).fetchone()[0]
            )
            if page_count < 1 or page_count != int(scan["pages_completed"]):
                raise ValueError("Auction discovery cannot complete without all page checkpoints")
            connection.execute(
                """
                UPDATE auction_discovery_runs
                SET status = 'completed', completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, timestamp, scan_id),
            )
            return True

    @staticmethod
    def _auction_bootstrap_work_from_row(
        row: sqlite3.Row,
    ) -> AuctionBootstrapWorkItem:
        try:
            listing_data = json.loads(str(row["listing_json"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("Stored auction bootstrap listing is invalid JSON") from exc
        if not isinstance(listing_data, dict):
            raise ValueError("Stored auction bootstrap listing must be a JSON object")
        return AuctionBootstrapWorkItem(
            exleasingcar_auction_id=str(row["exleasingcar_auction_id"]),
            priority=int(row["priority"]),
            status=str(row["status"]),
            attempt_count=int(row["attempt_count"]),
            listing_data=listing_data,
            last_error=(
                str(row["last_error"]) if row["last_error"] is not None else None
            ),
        )

    def enqueue_auction_bootstrap(
        self,
        listing_data: Mapping[str, Any],
        *,
        priority: int,
        observed_at: datetime | None = None,
    ) -> bool:
        """Add or refresh one discovered auction without reopening completed bootstrap work."""
        if not isinstance(listing_data, Mapping):
            raise TypeError("listing_data must be a mapping")
        auction_id = self._validate_identifier(
            listing_data.get("exleasingcar_auction_id"), "exleasingcar_auction_id"
        )
        source_url = self._validate_catalog_page_url(
            listing_data.get("source_url"), "source_url"
        )
        if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
            raise ValueError("priority must be a non-negative integer")
        try:
            serialized = json.dumps(
                dict(listing_data),
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("listing_data must be finite JSON-compatible data") from exc
        timestamp = self._serialize_datetime(observed_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            auction = connection.execute(
                """
                SELECT id FROM auctions WHERE exleasingcar_auction_id = ?
                """,
                (auction_id,),
            ).fetchone()
            if auction is None:
                raise KeyError(f"Auction {auction_id} must be upserted before enqueue")
            database_auction_id = int(auction["id"])
            bootstrap_completed = connection.execute(
                """
                SELECT 1 FROM catalog_scan_runs
                WHERE auction_id = ? AND scan_type = 'bootstrap'
                  AND status = 'completed'
                LIMIT 1
                """,
                (database_auction_id,),
            ).fetchone() is not None
            initial_status = "completed" if bootstrap_completed else "pending"
            connection.execute(
                """
                INSERT INTO auction_bootstrap_queue (
                    auction_id, source_url, listing_json, priority, status,
                    attempt_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(auction_id) DO UPDATE SET
                    source_url = excluded.source_url,
                    listing_json = excluded.listing_json,
                    priority = excluded.priority,
                    status = CASE
                        WHEN auction_bootstrap_queue.status = 'completed'
                          OR excluded.status = 'completed'
                        THEN 'completed'
                        ELSE auction_bootstrap_queue.status
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    database_auction_id,
                    source_url,
                    serialized,
                    priority,
                    initial_status,
                    timestamp,
                    timestamp,
                ),
            )
            return not bootstrap_completed

    def claim_next_auction_bootstrap_work(
        self,
        *,
        exleasingcar_auction_id: str | None = None,
        include_completed: bool = False,
        observed_at: datetime | None = None,
        stale_after_minutes: int = 2,
    ) -> AuctionBootstrapWorkItem | None:
        """Atomically claim one pending queue item; stale claims recover after crashes."""
        if not isinstance(include_completed, bool):
            raise TypeError("include_completed must be bool")
        if (
            isinstance(stale_after_minutes, bool)
            or not isinstance(stale_after_minutes, int)
            or stale_after_minutes < 1
        ):
            raise ValueError("stale_after_minutes must be a positive integer")
        timestamp = self._serialize_datetime(observed_at)
        now = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        stale_cutoff = self._serialize_datetime(
            now - timedelta(minutes=stale_after_minutes)
        )
        auction_id = (
            self._validate_identifier(exleasingcar_auction_id, "exleasingcar_auction_id")
            if exleasingcar_auction_id is not None
            else None
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE auction_bootstrap_queue
                SET status = 'pending', updated_at = ?, last_error = 'recovered_stale_claim'
                WHERE status = 'in_progress'
                  AND last_started_at IS NOT NULL
                  AND last_started_at <= ?
                """,
                (timestamp, stale_cutoff),
            )
            if auction_id is not None:
                row = connection.execute(
                    """
                    SELECT auctions.exleasingcar_auction_id, queue.priority,
                           queue.status, queue.attempt_count, queue.listing_json,
                           queue.last_error
                    FROM auction_bootstrap_queue AS queue
                    JOIN auctions ON auctions.id = queue.auction_id
                    WHERE auctions.exleasingcar_auction_id = ?
                      AND (queue.status = 'pending' OR ? = 1)
                    """,
                    (auction_id, int(include_completed)),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT auctions.exleasingcar_auction_id, queue.priority,
                           queue.status, queue.attempt_count, queue.listing_json,
                           queue.last_error
                    FROM auction_bootstrap_queue AS queue
                    JOIN auctions ON auctions.id = queue.auction_id
                    WHERE queue.status = 'pending'
                    ORDER BY queue.priority, queue.created_at,
                             queue.last_started_at, auctions.exleasingcar_auction_id
                    LIMIT 1
                    """
                ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE auction_bootstrap_queue
                SET status = 'in_progress', attempt_count = attempt_count + 1,
                    last_started_at = ?, updated_at = ?, last_error = NULL,
                    completed_at = NULL
                WHERE auction_id = (
                    SELECT id FROM auctions WHERE exleasingcar_auction_id = ?
                )
                """,
                (timestamp, timestamp, str(row["exleasingcar_auction_id"])),
            )
            claimed = connection.execute(
                """
                SELECT auctions.exleasingcar_auction_id, queue.priority,
                       queue.status, queue.attempt_count, queue.listing_json,
                       queue.last_error
                FROM auction_bootstrap_queue AS queue
                JOIN auctions ON auctions.id = queue.auction_id
                WHERE auctions.exleasingcar_auction_id = ?
                """,
                (str(row["exleasingcar_auction_id"]),),
            ).fetchone()
            assert claimed is not None
            return self._auction_bootstrap_work_from_row(claimed)

    def mark_auction_bootstrap_pending(
        self,
        exleasingcar_auction_id: str,
        *,
        error_kind: str | None = None,
        observed_at: datetime | None = None,
    ) -> bool:
        auction_id = self._validate_identifier(
            exleasingcar_auction_id, "exleasingcar_auction_id"
        )
        if error_kind is not None:
            if not isinstance(error_kind, str) or not error_kind.strip():
                raise ValueError("error_kind must be a non-empty string when supplied")
            error_kind = re.sub(r"[^A-Za-z0-9_.-]", "_", error_kind.strip())[:80]
        timestamp = self._serialize_datetime(observed_at)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE auction_bootstrap_queue
                SET status = 'pending', updated_at = ?, last_error = ?,
                    completed_at = NULL
                WHERE auction_id = (
                    SELECT id FROM auctions WHERE exleasingcar_auction_id = ?
                )
                """,
                (timestamp, error_kind, auction_id),
            )
            return cursor.rowcount == 1

    def mark_auction_bootstrap_completed(
        self,
        exleasingcar_auction_id: str,
        *,
        observed_at: datetime | None = None,
    ) -> bool:
        auction_id = self._validate_identifier(
            exleasingcar_auction_id, "exleasingcar_auction_id"
        )
        timestamp = self._serialize_datetime(observed_at)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE auction_bootstrap_queue
                SET status = 'completed', updated_at = ?, completed_at = ?,
                    last_error = NULL
                WHERE auction_id = (
                    SELECT id FROM auctions WHERE exleasingcar_auction_id = ?
                )
                """,
                (timestamp, timestamp, auction_id),
            )
            return cursor.rowcount == 1

    def get_auction_bootstrap_queue_summary(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS item_count
                FROM auction_bootstrap_queue
                GROUP BY status
                """
            ).fetchall()
        summary = {"pending": 0, "in_progress": 0, "completed": 0, "total": 0}
        for row in rows:
            status = str(row["status"])
            count = int(row["item_count"])
            summary[status] = count
            summary["total"] += count
        return summary

    def get_catalog_scan_pages(self, scan_id: int) -> tuple[CatalogScanPage, ...]:
        """Load all completed pages so a restarted collector can rebuild its work set."""
        if isinstance(scan_id, bool) or not isinstance(scan_id, int) or scan_id < 1:
            raise ValueError("scan_id must be a positive integer")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, scan_run_id, page_number, page_url, item_count,
                       completed_at, page_data_json, api_responses_json,
                       api_responses_processed
                FROM catalog_scan_pages
                WHERE scan_run_id = ?
                ORDER BY page_number, id
                """,
                (scan_id,),
            ).fetchall()
        return tuple(self._catalog_scan_page_from_row(row) for row in rows)

    def get_pending_catalog_api_pages(
        self,
        exleasingcar_auction_id: str,
    ) -> tuple[CatalogScanPage, ...]:
        """Return pages with captured API responses that have not all been consumed."""
        auction_id = self._validate_identifier(
            exleasingcar_auction_id, "exleasingcar_auction_id"
        )
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT pages.id, pages.scan_run_id, pages.page_number, pages.page_url,
                       pages.item_count, pages.completed_at, pages.page_data_json,
                       pages.api_responses_json, pages.api_responses_processed
                FROM catalog_scan_pages AS pages
                JOIN catalog_scan_runs AS runs ON runs.id = pages.scan_run_id
                JOIN auctions ON auctions.id = runs.auction_id
                WHERE auctions.exleasingcar_auction_id = ?
                  AND pages.api_responses_json != '[]'
                ORDER BY pages.completed_at, pages.id
                """,
                (auction_id,),
            ).fetchall()
        result = []
        for row in rows:
            page = self._catalog_scan_page_from_row(row)
            if page.api_responses_processed < len(page.api_responses):
                result.append(page)
        return tuple(result)

    def append_catalog_api_responses(
        self,
        page_id: int,
        api_responses: Sequence[Mapping[str, Any]],
    ) -> int:
        """Durably append API responses triggered by detail enrichment.

        Responses are stored on the catalogue page that introduced the lot so
        they remain associated with its auction and are replayable after a
        process restart. Exact duplicate envelopes are ignored.
        """
        if isinstance(page_id, bool) or not isinstance(page_id, int) or page_id < 1:
            raise ValueError("page_id must be a positive integer")
        normalized = self._normalize_catalog_api_responses(api_responses)
        if not normalized:
            return 0

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT api_responses_json, api_responses_processed
                FROM catalog_scan_pages
                WHERE id = ?
                """,
                (page_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown catalogue page id: {page_id}")
            try:
                existing = json.loads(str(row["api_responses_json"]))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Invalid persisted API response queue") from exc
            if not isinstance(existing, list) or any(
                not isinstance(item, Mapping) for item in existing
            ):
                raise TypeError("Invalid persisted API response queue")

            existing_signatures = {
                json.dumps(item, ensure_ascii=False, sort_keys=True, allow_nan=False)
                for item in existing
            }
            appended = []
            for item in normalized:
                signature = json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                )
                if signature in existing_signatures:
                    continue
                existing_signatures.add(signature)
                appended.append(item)
            if not appended:
                return 0

            connection.execute(
                """
                UPDATE catalog_scan_pages
                SET api_responses_json = ?
                WHERE id = ?
                """,
                (
                    json.dumps(
                        [*existing, *appended],
                        ensure_ascii=False,
                        sort_keys=True,
                        allow_nan=False,
                    ),
                    page_id,
                ),
            )
            return len(appended)

    def advance_catalog_api_response_cursor(
        self,
        page_id: int,
        expected_response_index: int,
    ) -> bool:
        """Mark exactly one stored API response consumed using compare-and-swap."""
        for value, name in (
            (page_id, "page_id"),
            (expected_response_index, "expected_response_index"),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < (1 if name == "page_id" else 0):
                raise ValueError(f"{name} is out of range")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT api_responses_processed, api_responses_json
                FROM catalog_scan_pages
                WHERE id = ?
                """,
                (page_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown catalogue page id: {page_id}")
            try:
                responses = json.loads(str(row["api_responses_json"]))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Invalid persisted API response queue") from exc
            if not isinstance(responses, list):
                raise TypeError("Invalid persisted API response queue")
            current = int(row["api_responses_processed"])
            if current > expected_response_index:
                return False
            if current < expected_response_index:
                raise ValueError("API response cursor moved; reload the pending page")
            if current >= len(responses):
                return False
            connection.execute(
                """
                UPDATE catalog_scan_pages
                SET api_responses_processed = api_responses_processed + 1
                WHERE id = ? AND api_responses_processed = ?
                """,
                (page_id, expected_response_index),
            )
            return True

    def get_auction_lot_sale_types(
        self,
        exleasingcar_auction_id: str,
        source_car_ids: set[str],
    ) -> dict[str, str | None]:
        """Return known sale modes, including NULL for unclassified catalogue lots."""
        auction_id = self._validate_identifier(
            exleasingcar_auction_id, "exleasingcar_auction_id"
        )
        car_ids = sorted(
            {self._validate_identifier(car_id, "source_car_id") for car_id in source_car_ids}
        )
        if not car_ids:
            return {}
        placeholders = ", ".join("?" for _ in car_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT vehicles.source_car_id, auction_lots.sale_type
                FROM auction_lots
                JOIN vehicles ON vehicles.id = auction_lots.vehicle_id
                JOIN auctions ON auctions.id = auction_lots.auction_id
                WHERE auctions.exleasingcar_auction_id = ?
                  AND vehicles.source_car_id IN ({placeholders})
                """,
                (auction_id, *car_ids),
            ).fetchall()
        return {
            str(row["source_car_id"]): (
                str(row["sale_type"]) if row["sale_type"] is not None else None
            )
            for row in rows
        }


    def defer_catalog_api_lot_updates(
        self,
        page_id: int,
        api_response: Mapping[str, Any],
        source_car_ids: set[str],
    ) -> int:
        """Split unknown-mode lots out of a bulk response without blocking known lots."""
        if isinstance(page_id, bool) or not isinstance(page_id, int) or page_id < 1:
            raise ValueError("page_id must be a positive integer")
        if not isinstance(api_response, Mapping):
            raise TypeError("api_response must be a mapping")
        normalized_responses = self._normalize_catalog_api_responses((api_response,))
        if len(normalized_responses) != 1:
            raise ValueError("exactly one API response is required")
        normalized = normalized_responses[0]
        payload = normalized["payload"]
        car_ids = sorted(
            {self._validate_identifier(car_id, "source_car_id") for car_id in source_car_ids}
        )
        entries = []
        for car_id in car_ids:
            if car_id not in payload or payload[car_id] is None:
                continue
            payload_json = json.dumps(
                {car_id: payload[car_id]},
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
            entries.append(
                (
                    page_id,
                    car_id,
                    normalized["url"],
                    payload_json,
                    normalized["observed_at"],
                )
            )
        if not entries:
            return 0

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM catalog_scan_pages WHERE id = ?",
                (page_id,),
            ).fetchone() is None:
                raise KeyError(f"Unknown catalogue page id: {page_id}")
            cursor = connection.executemany(
                """
                INSERT INTO catalog_deferred_api_lots (
                    page_id, source_car_id, source_url,
                    response_payload_json, observed_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (
                    page_id, source_car_id, source_url, response_payload_json, observed_at
                ) DO NOTHING
                """,
                entries,
            )
            return max(0, cursor.rowcount)

    def get_catalog_deferred_api_pages(
        self,
        exleasingcar_auction_id: str,
    ) -> tuple[CatalogScanPage, ...]:
        """Return checkpoint pages that still own unprocessed deferred lot updates.

        Unlike get_pending_catalog_api_pages(), this includes entries whose sale
        mode is still unknown so their saved detail URL and lot metadata can be
        restored even after the lot disappears from the current catalogue.
        """
        auction_id = self._validate_identifier(
            exleasingcar_auction_id, "exleasingcar_auction_id"
        )
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT pages.id, pages.scan_run_id, pages.page_number,
                       pages.page_url, pages.item_count, pages.completed_at,
                       pages.page_data_json, pages.api_responses_json,
                       pages.api_responses_processed
                FROM catalog_scan_pages AS pages
                JOIN catalog_scan_runs AS runs ON runs.id = pages.scan_run_id
                JOIN auctions ON auctions.id = runs.auction_id
                JOIN catalog_deferred_api_lots AS deferred
                  ON deferred.page_id = pages.id
                WHERE auctions.exleasingcar_auction_id = ?
                  AND deferred.processed_at IS NULL
                ORDER BY pages.completed_at, pages.id
                """,
                (auction_id,),
            ).fetchall()
        return tuple(self._catalog_scan_page_from_row(row) for row in rows)

    def get_pending_catalog_api_lot_updates(
        self,
        exleasingcar_auction_id: str,
    ) -> tuple[dict[str, Any], ...]:
        """Return deferred payloads only after their auction lot has a known sale mode."""
        auction_id = self._validate_identifier(
            exleasingcar_auction_id, "exleasingcar_auction_id"
        )
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT deferred.id, deferred.page_id, deferred.source_car_id,
                       deferred.source_url, deferred.response_payload_json,
                       deferred.observed_at
                FROM catalog_deferred_api_lots AS deferred
                JOIN catalog_scan_pages AS pages ON pages.id = deferred.page_id
                JOIN catalog_scan_runs AS runs ON runs.id = pages.scan_run_id
                JOIN auctions ON auctions.id = runs.auction_id
                JOIN auction_lots ON auction_lots.auction_id = auctions.id
                JOIN vehicles ON vehicles.id = auction_lots.vehicle_id
                              AND vehicles.source_car_id = deferred.source_car_id
                WHERE auctions.exleasingcar_auction_id = ?
                  AND deferred.processed_at IS NULL
                  AND auction_lots.sale_type IS NOT NULL
                ORDER BY deferred.observed_at, deferred.id
                """,
                (auction_id,),
            ).fetchall()
        result = []
        for row in rows:
            try:
                payload = json.loads(str(row["response_payload_json"]))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Invalid deferred API payload for lot {row['source_car_id']}"
                ) from exc
            if not isinstance(payload, Mapping):
                raise TypeError("Invalid deferred API payload shape")
            result.append(
                {
                    "id": int(row["id"]),
                    "page_id": int(row["page_id"]),
                    "source_car_id": str(row["source_car_id"]),
                    "url": str(row["source_url"]),
                    "payload": payload,
                    "observed_at": str(row["observed_at"]),
                }
            )
        return tuple(result)

    def mark_catalog_api_lot_update_processed(
        self,
        deferred_update_id: int,
        *,
        observed_at: datetime | None = None,
    ) -> bool:
        """Mark one deferred lot payload processed after its archive writes commit."""
        if (
            isinstance(deferred_update_id, bool)
            or not isinstance(deferred_update_id, int)
            or deferred_update_id < 1
        ):
            raise ValueError("deferred_update_id must be a positive integer")
        timestamp = self._serialize_datetime(observed_at)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE catalog_deferred_api_lots
                SET processed_at = ?
                WHERE id = ? AND processed_at IS NULL
                """,
                (timestamp, deferred_update_id),
            )
            return cursor.rowcount > 0

    def complete_catalog_scan(
        self,
        scan_id: int,
        *,
        observed_at: datetime | None = None,
    ) -> bool:
        """Mark a scan complete only after its final page has been checkpointed."""
        if isinstance(scan_id, bool) or not isinstance(scan_id, int) or scan_id < 1:
            raise ValueError("scan_id must be a positive integer")
        timestamp = self._serialize_datetime(observed_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            scan = connection.execute(
                """
                SELECT status, next_page_url, next_page_number, pages_completed
                FROM catalog_scan_runs
                WHERE id = ?
                """,
                (scan_id,),
            ).fetchone()
            if scan is None:
                raise KeyError(f"Unknown catalogue scan id: {scan_id}")
            if str(scan["status"]) == "completed":
                return False
            if scan["next_page_url"] is not None or scan["next_page_number"] is not None:
                raise ValueError("Cannot complete a catalogue scan while pages remain")
            page_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM catalog_scan_pages WHERE scan_run_id = ?",
                    (scan_id,),
                ).fetchone()[0]
            )
            if page_count < 1 or page_count != int(scan["pages_completed"]):
                raise ValueError("Cannot complete a scan without all page checkpoints")
            connection.execute(
                """
                UPDATE catalog_scan_runs
                SET status = 'completed', completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, timestamp, scan_id),
            )
            return True

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
        *,
        recheck_after_days: int = 7,
    ) -> list[str]:
        """Refresh detail metadata weekly; API snapshots still flow on each catalogue run."""
        auction_id = self._validate_identifier(
            exleasingcar_auction_id, "exleasingcar_auction_id"
        )
        if isinstance(recheck_after_days, bool) or not isinstance(recheck_after_days, int):
            raise TypeError("recheck_after_days must be an integer")
        if recheck_after_days < 1:
            raise ValueError("recheck_after_days must be at least one day")
        car_ids = sorted(
            {self._validate_identifier(car_id, "source_car_id") for car_id in source_car_ids}
        )
        if not car_ids:
            return []
        placeholders = ", ".join("?" for _ in car_ids)
        cutoff = self._serialize_datetime(
            datetime.now(timezone.utc) - timedelta(days=recheck_after_days)
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT v.source_car_id
                FROM auction_lots AS l
                JOIN vehicles AS v ON v.id = l.vehicle_id
                JOIN auctions AS a ON a.id = l.auction_id
                WHERE a.exleasingcar_auction_id = ?
                  AND v.source_car_id IN ({placeholders})
                  AND NOT (
                      l.sale_type = 'open_auction'
                      AND l.monitoring_completed_at IS NOT NULL
                  )
                  AND (
                      MAX(
                          COALESCE(l.last_detail_checked_at, ''),
                          COALESCE(l.last_detail_attempted_at, '')
                      ) = ''
                      OR MAX(
                          COALESCE(l.last_detail_checked_at, ''),
                          COALESCE(l.last_detail_attempted_at, '')
                      ) <= ?
                  )
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
                (auction_id, *car_ids, cutoff),
            ).fetchall()
            existing_rows = connection.execute(
                f"""
                SELECT v.source_car_id
                FROM auction_lots AS l
                JOIN vehicles AS v ON v.id = l.vehicle_id
                JOIN auctions AS a ON a.id = l.auction_id
                WHERE a.exleasingcar_auction_id = ?
                  AND v.source_car_id IN ({placeholders})
                """,
                (auction_id, *car_ids),
            ).fetchall()
        ordered = [str(row["source_car_id"]) for row in rows]
        existing = {str(row["source_car_id"]) for row in existing_rows}
        missing = sorted(set(car_ids) - existing)
        return ordered + missing

    def set_auction_lot_qualification(
        self,
        source_car_id: str,
        exleasingcar_auction_id: str,
        *,
        sale_type: SaleType | str | None,
        has_fixed_price: bool,
        collection_eligible: bool,
        checked_at: datetime | None = None,
    ) -> bool:
        """Persist the latest detail-page qualification for one auction lot."""
        car_id = self._validate_identifier(source_car_id, "source_car_id")
        auction_id = self._validate_identifier(
            exleasingcar_auction_id, "exleasingcar_auction_id"
        )
        if sale_type is None:
            normalized_sale_type = None
        else:
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
        if not isinstance(has_fixed_price, bool):
            raise TypeError("has_fixed_price must be bool")
        if not isinstance(collection_eligible, bool):
            raise TypeError("collection_eligible must be bool")
        timestamp = self._serialize_datetime(checked_at)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE auction_lots
                SET sale_type = ?,
                    has_fixed_price = ?,
                    collection_eligible = CASE
                        WHEN sale_type = ? AND collection_eligible = 1 THEN 1
                        ELSE ?
                    END,
                    last_detail_checked_at = ?
                WHERE vehicle_id = (
                    SELECT id FROM vehicles WHERE source_car_id = ?
                )
                AND auction_id = (
                    SELECT id FROM auctions WHERE exleasingcar_auction_id = ?
                )
                """,
                (
                    normalized_sale_type,
                    int(has_fixed_price),
                    normalized_sale_type,
                    int(collection_eligible),
                    timestamp,
                    car_id,
                    auction_id,
                ),
            )
            return cursor.rowcount > 0

    def mark_open_auction_completed(
        self,
        source_car_id: str,
        exleasingcar_auction_id: str,
        *,
        reason: str,
        completed_at: datetime | None = None,
    ) -> bool:
        """Freeze monitoring after a conclusive terminal event, preserving history."""
        car_id = self._validate_identifier(source_car_id, "source_car_id")
        auction_id = self._validate_identifier(
            exleasingcar_auction_id, "exleasingcar_auction_id"
        )
        allowed_reasons = {
            "countdown_expired",
            "api_terminal_status",
            "catalog_terminal_status",
        }
        if reason not in allowed_reasons:
            raise ValueError("Unsupported auction completion reason")
        timestamp = self._serialize_datetime(completed_at)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE auction_lots
                SET monitoring_completed_at = COALESCE(monitoring_completed_at, ?),
                    monitoring_completion_reason = COALESCE(
                        monitoring_completion_reason, ?
                    )
                WHERE vehicle_id = (
                    SELECT id FROM vehicles WHERE source_car_id = ?
                )
                  AND auction_id = (
                    SELECT id FROM auctions WHERE exleasingcar_auction_id = ?
                )
                  AND sale_type = 'open_auction'
                  AND collection_eligible = 1
                """,
                (timestamp, reason, car_id, auction_id),
            )
            return cursor.rowcount > 0

    def get_completed_open_auction_lots(
        self,
        exleasingcar_auction_id: str,
        source_car_ids: set[str],
    ) -> set[str]:
        """Return open lots whose final state has already been captured."""
        auction_id = self._validate_identifier(
            exleasingcar_auction_id, "exleasingcar_auction_id"
        )
        car_ids = sorted(
            {self._validate_identifier(car_id, "source_car_id") for car_id in source_car_ids}
        )
        if not car_ids:
            return set()
        placeholders = ", ".join("?" for _ in car_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT v.source_car_id
                FROM auction_lots AS al
                JOIN vehicles AS v ON v.id = al.vehicle_id
                JOIN auctions AS a ON a.id = al.auction_id
                WHERE a.exleasingcar_auction_id = ?
                  AND v.source_car_id IN ({placeholders})
                  AND al.sale_type = 'open_auction'
                  AND al.monitoring_completed_at IS NOT NULL
                """,
                (auction_id, *car_ids),
            ).fetchall()
        return {str(row["source_car_id"]) for row in rows}

    def get_collection_eligible_lots(
        self,
        exleasingcar_auction_id: str,
        source_car_ids: set[str],
    ) -> dict[str, str | None]:
        """Return previously qualified lot IDs and their preserved bidding mode."""
        auction_id = self._validate_identifier(
            exleasingcar_auction_id, "exleasingcar_auction_id"
        )
        car_ids = sorted(
            {self._validate_identifier(car_id, "source_car_id") for car_id in source_car_ids}
        )
        if not car_ids:
            return {}
        placeholders = ", ".join("?" for _ in car_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT v.source_car_id, al.sale_type
                FROM auction_lots AS al
                JOIN vehicles AS v ON v.id = al.vehicle_id
                JOIN auctions AS a ON a.id = al.auction_id
                WHERE a.exleasingcar_auction_id = ?
                  AND v.source_car_id IN ({placeholders})
                  AND al.collection_eligible = 1
                """,
                (auction_id, *car_ids),
            ).fetchall()
        return {str(row["source_car_id"]): row["sale_type"] for row in rows}

    def get_open_auction_lot_ids(
        self,
        exleasingcar_auction_id: str,
        source_car_ids: set[str],
    ) -> set[str]:
        """Return known open-auction lots, including candidates not yet qualified."""
        auction_id = self._validate_identifier(
            exleasingcar_auction_id, "exleasingcar_auction_id"
        )
        car_ids = sorted(
            {
                self._validate_identifier(car_id, "source_car_id")
                for car_id in source_car_ids
            }
        )
        if not car_ids:
            return set()
        placeholders = ", ".join("?" for _ in car_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT v.source_car_id
                FROM auction_lots AS al
                JOIN vehicles AS v ON v.id = al.vehicle_id
                JOIN auctions AS a ON a.id = al.auction_id
                WHERE a.exleasingcar_auction_id = ?
                  AND v.source_car_id IN ({placeholders})
                  AND al.sale_type = 'open_auction'
                  AND al.monitoring_completed_at IS NULL
                """,
                (auction_id, *car_ids),
            ).fetchall()
        return {str(row["source_car_id"]) for row in rows}

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

    def mark_api_checked(
        self,
        source_car_id: str,
        exleasingcar_auction_id: str,
        checked_at: datetime | None = None,
    ) -> bool:
        """Record an API response check even when no price value was returned."""
        car_id = self._validate_identifier(source_car_id, "source_car_id")
        auction_id = self._validate_identifier(
            exleasingcar_auction_id, "exleasingcar_auction_id"
        )
        timestamp = self._serialize_datetime(checked_at)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE auction_lots
                SET last_api_checked_at = CASE
                    WHEN last_api_checked_at IS NULL OR last_api_checked_at < ?
                    THEN ?
                    ELSE last_api_checked_at
                END
                WHERE vehicle_id = (
                    SELECT id FROM vehicles WHERE source_car_id = ?
                )
                AND auction_id = (
                    SELECT id FROM auctions WHERE exleasingcar_auction_id = ?
                )
                """,
                (timestamp, timestamp, car_id, auction_id),
            )
            return cursor.rowcount > 0

    @staticmethod
    def _validate_media_url(value: str | None, field_name: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} cannot be empty")
        normalized = value.strip()
        try:
            parts = urlsplit(normalized)
            port = parts.port
        except ValueError as exc:
            raise ValueError(
                f"{field_name} must be an HTTPS Exleasingcar media URL"
            ) from exc
        host = (parts.hostname or "").casefold()
        if (
            parts.scheme.casefold() != "https"
            or host not in {
                "data.exleasingcar.com",
                "www.exleasingcar.com",
                "portal.macadam.eu",
            }
            or parts.username is not None
            or parts.password is not None
            or port not in (None, 443)
            or not parts.path
            or parts.fragment
        ):
            raise ValueError(f"{field_name} must be an HTTPS Exleasingcar media URL")
        if host == "portal.macadam.eu":
            query = parse_qs(parts.query, keep_blank_values=True)
            if (
                parts.path != "/reports/remarketing"
                or set(query) != {"id", "culture"}
                or len(query.get("id", ())) != 1
                or len(query.get("culture", ())) != 1
                or not query["id"][0]
                or not query["culture"][0]
            ):
                raise ValueError(
                    f"{field_name} must be a valid MACADAM remarketing report URL"
                )
        return normalized

    def upsert_lot_media_source(
        self,
        source_car_id: str,
        exleasingcar_auction_id: str,
        media_type: str,
        source_url: str,
        *,
        preview_url: str | None = None,
        label: str | None = None,
        source_order: int = 0,
        discovered_at: datetime | None = None,
    ) -> int:
        """Register a photo/report URL against an existing auction lot."""
        car_id = self._validate_identifier(source_car_id, "source_car_id")
        auction_external_id = self._validate_identifier(
            exleasingcar_auction_id, "exleasingcar_auction_id"
        )
        if media_type not in {"photo", "report"}:
            raise ValueError("media_type must be 'photo' or 'report'")
        normalized_url = self._validate_media_url(source_url, "source_url")
        normalized_preview_url = self._validate_media_url(
            preview_url, "preview_url"
        )
        if isinstance(source_order, bool) or not isinstance(source_order, int):
            raise TypeError("source_order must be an integer")
        if source_order < 0:
            raise ValueError("source_order cannot be negative")
        if label is not None:
            if not isinstance(label, str):
                raise TypeError("label must be str or None")
            label = label.strip()[:250] or None
        timestamp = self._serialize_datetime(discovered_at)

        with self._connect() as connection:
            lot = connection.execute(
                """
                SELECT al.id
                FROM auction_lots al
                JOIN vehicles v ON v.id = al.vehicle_id
                JOIN auctions a ON a.id = al.auction_id
                WHERE v.source_car_id = ?
                  AND a.exleasingcar_auction_id = ?
                """,
                (car_id, auction_external_id),
            ).fetchone()
            if lot is None:
                raise ValueError(
                    "Cannot register media for a vehicle/auction pair "
                    "that is not already stored as a lot"
                )
            lot_id = int(lot["id"])
            connection.execute(
                """
                INSERT INTO lot_media_sources (
                    auction_lot_id, media_type, source_url, preview_url,
                    label, source_order, download_status,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'discovered', ?, ?)
                ON CONFLICT(auction_lot_id, media_type, source_url) DO UPDATE SET
                    preview_url = COALESCE(
                        excluded.preview_url, lot_media_sources.preview_url
                    ),
                    label = COALESCE(excluded.label, lot_media_sources.label),
                    source_order = excluded.source_order,
                    last_seen_at = MAX(
                        lot_media_sources.last_seen_at, excluded.last_seen_at
                    ),
                    download_status = CASE
                        WHEN lot_media_sources.current_asset_id IS NULL
                        THEN 'discovered'
                        ELSE 'downloaded'
                    END,
                    last_error = CASE
                        WHEN lot_media_sources.current_asset_id IS NULL
                        THEN NULL
                        ELSE lot_media_sources.last_error
                    END
                """,
                (
                    lot_id,
                    media_type,
                    normalized_url,
                    normalized_preview_url,
                    label,
                    source_order,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                """
                SELECT id FROM lot_media_sources
                WHERE auction_lot_id = ? AND media_type = ? AND source_url = ?
                """,
                (lot_id, media_type, normalized_url),
            ).fetchone()
            if row is None:
                raise RuntimeError("Failed to register lot media source")
            return int(row["id"])

    def get_pending_media_sources(
        self,
        limit: int = 20,
        *,
        include_downloaded: bool = False,
    ) -> list[dict[str, object]]:
        """Return a bounded list of lot-scoped media URLs awaiting download.

        Downloaded sources are excluded by default. The refresh flag is
        intended for an explicit pass that checks whether archived URLs changed.
        """
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if not isinstance(include_downloaded, bool):
            raise TypeError("include_downloaded must be a boolean")

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.id AS media_source_id,
                       m.media_type,
                       m.source_url,
                       m.preview_url,
                       m.label,
                       m.source_order,
                       m.download_status,
                       v.source_car_id,
                       a.exleasingcar_auction_id AS auction_id
                FROM lot_media_sources AS m
                JOIN auction_lots AS al ON al.id = m.auction_lot_id
                JOIN vehicles AS v ON v.id = al.vehicle_id
                JOIN auctions AS a ON a.id = al.auction_id
                WHERE al.collection_eligible = 1
                  AND (
                    (
                        m.current_asset_id IS NULL
                        AND m.download_status IN ('discovered', 'failed')
                    )
                    OR (? = 1 AND m.current_asset_id IS NOT NULL)
                )
                ORDER BY m.id
                LIMIT ?
                """,
                (int(include_downloaded), limit),
            )
            return [dict(row) for row in rows]

    def mark_media_source_attempted(
        self,
        media_source_id: int,
        attempted_at: datetime | None = None,
    ) -> bool:
        if isinstance(media_source_id, bool) or not isinstance(media_source_id, int):
            raise TypeError("media_source_id must be an integer")
        timestamp = self._serialize_datetime(attempted_at)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE lot_media_sources
                SET last_attempted_at = ?
                WHERE id = ?
                """,
                (timestamp, media_source_id),
            )
            return cursor.rowcount > 0

    def mark_media_source_failed(
        self,
        media_source_id: int,
        error: str,
        attempted_at: datetime | None = None,
    ) -> bool:
        if isinstance(media_source_id, bool) or not isinstance(media_source_id, int):
            raise TypeError("media_source_id must be an integer")
        if not isinstance(error, str) or not error.strip():
            raise ValueError("error cannot be empty")
        timestamp = self._serialize_datetime(attempted_at)
        safe_error = error.strip()[:500]
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE lot_media_sources
                SET download_status = CASE
                        WHEN current_asset_id IS NULL THEN 'failed'
                        ELSE 'downloaded'
                    END,
                    last_attempted_at = ?,
                    last_error = ?
                WHERE id = ?
                """,
                (timestamp, safe_error, media_source_id),
            )
            return cursor.rowcount > 0

    def register_media_asset_for_source(
        self,
        media_source_id: int,
        *,
        sha256: str,
        object_key: str,
        mime_type: str,
        byte_size: int,
        original_filename: str | None = None,
        downloaded_at: datetime | None = None,
    ) -> int:
        """Attach an immutable, content-addressed object version to a lot source."""
        if isinstance(media_source_id, bool) or not isinstance(media_source_id, int):
            raise TypeError("media_source_id must be an integer")
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("sha256 must be a lowercase 64-character hex digest")
        if not isinstance(object_key, str) or not object_key.strip():
            raise ValueError("object_key cannot be empty")
        normalized_key = object_key.strip().replace("\\", "/")
        if normalized_key.startswith("/") or any(
            part in {".", ".."} for part in normalized_key.split("/")
        ):
            raise ValueError("object_key must be a safe relative object key")
        if not isinstance(mime_type, str) or not mime_type.strip():
            raise ValueError("mime_type cannot be empty")
        if isinstance(byte_size, bool) or not isinstance(byte_size, int):
            raise TypeError("byte_size must be an integer")
        if byte_size < 0:
            raise ValueError("byte_size cannot be negative")
        if original_filename is not None and not isinstance(original_filename, str):
            raise TypeError("original_filename must be str or None")
        timestamp = self._serialize_datetime(downloaded_at)

        with self._connect() as connection:
            source = connection.execute(
                "SELECT id FROM lot_media_sources WHERE id = ?",
                (media_source_id,),
            ).fetchone()
            if source is None:
                raise ValueError(f"Unknown media source ID: {media_source_id}")
            connection.execute(
                """
                INSERT INTO media_assets (
                    sha256, object_key, mime_type, byte_size,
                    original_filename, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(sha256) DO NOTHING
                """,
                (
                    sha256,
                    normalized_key,
                    mime_type.strip().lower(),
                    byte_size,
                    original_filename.strip()[:255] if original_filename else None,
                    timestamp,
                ),
            )
            asset = connection.execute(
                "SELECT id, byte_size FROM media_assets WHERE sha256 = ?",
                (sha256,),
            ).fetchone()
            if asset is None:
                raise RuntimeError("Failed to register media asset")
            if int(asset["byte_size"]) != byte_size:
                raise ValueError("Existing SHA-256 asset has a conflicting byte size")
            asset_id = int(asset["id"])
            connection.execute(
                """
                INSERT INTO media_asset_versions (
                    lot_media_source_id, media_asset_id, linked_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(lot_media_source_id, media_asset_id) DO NOTHING
                """,
                (media_source_id, asset_id, timestamp),
            )
            connection.execute(
                """
                UPDATE lot_media_sources
                SET current_asset_id = ?,
                    download_status = 'downloaded',
                    last_downloaded_at = ?,
                    last_attempted_at = ?,
                    last_error = NULL
                WHERE id = ?
                """,
                (asset_id, timestamp, timestamp, media_source_id),
            )
            return asset_id


    def get_media_asset_by_sha256(self, sha256: str) -> dict[str, object] | None:
        """Return an already registered content-addressed media asset, if present."""
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise ValueError("sha256 must be a lowercase 64-character digest")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, sha256, object_key, mime_type, byte_size, original_filename
                FROM media_assets
                WHERE sha256 = ?
                """,
                (sha256,),
            ).fetchone()
            return dict(row) if row is not None else None

    def get_media_derivative(
        self,
        source_asset_id: int,
        variant: str,
        transform_version: str,
        settings_json: str,
    ) -> dict[str, object] | None:
        """Return one derivative record for an original asset and exact transform."""
        if (
            isinstance(source_asset_id, bool)
            or not isinstance(source_asset_id, int)
            or source_asset_id <= 0
        ):
            raise ValueError("source_asset_id must be a positive integer")
        if variant not in {"card", "gallery"}:
            raise ValueError("variant must be 'card' or 'gallery'")
        if not isinstance(transform_version, str) or not transform_version.strip():
            raise ValueError("transform_version cannot be empty")
        if not isinstance(settings_json, str) or not settings_json.strip():
            raise ValueError("settings_json cannot be empty")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT d.id, d.source_asset_id, d.variant, d.transform_version,
                       d.settings_json, d.status, d.derived_asset_id,
                       d.width, d.height, d.last_attempted_at, d.last_error,
                       asset.sha256 AS derived_sha256,
                       asset.object_key AS derived_object_key,
                       asset.mime_type AS derived_mime_type,
                       asset.byte_size AS derived_byte_size
                FROM media_derivatives d
                LEFT JOIN media_assets asset ON asset.id = d.derived_asset_id
                WHERE d.source_asset_id = ?
                  AND d.variant = ?
                  AND d.transform_version = ?
                  AND d.settings_json = ?
                """,
                (source_asset_id, variant, transform_version, settings_json),
            ).fetchone()
            return dict(row) if row is not None else None

    def register_media_derivative(
        self,
        source_asset_id: int,
        variant: str,
        transform_version: str,
        settings_json: str,
        *,
        status: str,
        derived_sha256: str | None = None,
        object_key: str | None = None,
        mime_type: str | None = None,
        byte_size: int | None = None,
        width: int | None = None,
        height: int | None = None,
        error: str | None = None,
        attempted_at: datetime | None = None,
    ) -> int:
        """Register a ready derivative or an isolated per-asset processing result."""
        if (
            isinstance(source_asset_id, bool)
            or not isinstance(source_asset_id, int)
            or source_asset_id <= 0
        ):
            raise ValueError("source_asset_id must be a positive integer")
        if variant not in {"card", "gallery"}:
            raise ValueError("variant must be 'card' or 'gallery'")
        if not isinstance(transform_version, str) or not transform_version.strip():
            raise ValueError("transform_version cannot be empty")
        if len(transform_version) > 64:
            raise ValueError("transform_version is too long")
        if (
            not isinstance(settings_json, str)
            or not settings_json.strip()
            or len(settings_json) > 1000
        ):
            raise ValueError("settings_json must be a non-empty string of at most 1000 characters")
        if status not in {"ready", "unsupported", "failed"}:
            raise ValueError("status must be 'ready', 'unsupported' or 'failed'")

        normalized_key: str | None = None
        normalized_mime: str | None = None
        safe_error: str | None = None
        if status == "ready":
            if (
                not isinstance(derived_sha256, str)
                or len(derived_sha256) != 64
                or any(character not in "0123456789abcdef" for character in derived_sha256)
            ):
                raise ValueError("derived_sha256 must be a lowercase 64-character digest")
            if not isinstance(object_key, str) or not object_key.strip():
                raise ValueError("object_key is required for a ready derivative")
            normalized_key = object_key.strip().replace("\\\\", "/")
            if normalized_key != (
                f"archive/photo/{derived_sha256[:2]}/{derived_sha256}"
            ):
                raise ValueError("object_key must be a safe content-addressed photo key")
            if not isinstance(mime_type, str) or mime_type.strip().lower() != "image/jpeg":
                raise ValueError("derivative mime_type must be image/jpeg")
            normalized_mime = "image/jpeg"
            for name, value in (("byte_size", byte_size), ("width", width), ("height", height)):
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise ValueError(f"{name} must be a positive integer")
            if error is not None:
                raise ValueError("error must be None for a ready derivative")
        else:
            if any(
                value is not None
                for value in (
                    derived_sha256,
                    object_key,
                    mime_type,
                    byte_size,
                    width,
                    height,
                )
            ):
                raise ValueError("asset details must be omitted for a failed derivative")
            if not isinstance(error, str) or not error.strip():
                raise ValueError("error is required for an unsupported or failed derivative")
            safe_error = error.strip()[:250]

        timestamp = self._serialize_datetime(attempted_at)
        with self._connect() as connection:
            source = connection.execute(
                "SELECT mime_type FROM media_assets WHERE id = ?",
                (source_asset_id,),
            ).fetchone()
            if source is None:
                raise ValueError("source media asset does not exist")
            if not str(source["mime_type"]).lower().startswith("image/"):
                raise ValueError("source media asset must be an image")

            derived_asset_id: int | None = None
            if status == "ready":
                connection.execute(
                    """
                    INSERT INTO media_assets (
                        sha256, object_key, mime_type, byte_size,
                        original_filename, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(sha256) DO NOTHING
                    """,
                    (
                        derived_sha256,
                        normalized_key,
                        normalized_mime,
                        byte_size,
                        f"{variant}.jpg",
                        timestamp,
                    ),
                )
                asset = connection.execute(
                    """
                    SELECT id, object_key, mime_type, byte_size
                    FROM media_assets
                    WHERE sha256 = ?
                    """,
                    (derived_sha256,),
                ).fetchone()
                if asset is None:
                    raise RuntimeError("Failed to register derivative media asset")
                if (
                    str(asset["object_key"]) != normalized_key
                    or str(asset["mime_type"]).lower() != normalized_mime
                    or int(asset["byte_size"]) != byte_size
                ):
                    raise ValueError("Existing derivative asset metadata conflicts with its digest")
                derived_asset_id = int(asset["id"])

            connection.execute(
                """
                INSERT INTO media_derivatives (
                    source_asset_id, variant, transform_version, settings_json,
                    status, derived_asset_id, width, height,
                    last_attempted_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_asset_id, variant, transform_version, settings_json)
                DO UPDATE SET
                    status = excluded.status,
                    derived_asset_id = excluded.derived_asset_id,
                    width = excluded.width,
                    height = excluded.height,
                    last_attempted_at = excluded.last_attempted_at,
                    last_error = excluded.last_error
                """,
                (
                    source_asset_id,
                    variant,
                    transform_version,
                    settings_json,
                    status,
                    derived_asset_id,
                    width if status == "ready" else None,
                    height if status == "ready" else None,
                    timestamp,
                    safe_error,
                ),
            )
            row = connection.execute(
                """
                SELECT id FROM media_derivatives
                WHERE source_asset_id = ? AND variant = ?
                  AND transform_version = ? AND settings_json = ?
                """,
                (source_asset_id, variant, transform_version, settings_json),
            ).fetchone()
            if row is None:
                raise RuntimeError("Failed to register media derivative")
            return int(row["id"])

    def get_lot_media_sources(
        self,
        source_car_id: str,
        exleasingcar_auction_id: str | None = None,
    ) -> list[dict[str, object]]:
        car_id = self._validate_identifier(source_car_id, "source_car_id")
        parameters: list[str] = [car_id]
        auction_filter = ""
        if exleasingcar_auction_id is not None:
            auction_filter = " AND a.exleasingcar_auction_id = ?"
            parameters.append(
                self._validate_identifier(
                    exleasingcar_auction_id, "exleasingcar_auction_id"
                )
            )
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT a.exleasingcar_auction_id AS auction_id,
                       m.id AS media_source_id, m.media_type, m.source_url,
                       m.preview_url, m.label, m.source_order, m.download_status,
                       m.first_seen_at, m.last_seen_at, m.last_downloaded_at,
                       m.last_error, asset.sha256, asset.object_key,
                       asset.mime_type, asset.byte_size, asset.original_filename
                FROM lot_media_sources m
                JOIN auction_lots al ON al.id = m.auction_lot_id
                JOIN vehicles v ON v.id = al.vehicle_id
                LEFT JOIN auctions a ON a.id = al.auction_id
                LEFT JOIN media_assets asset ON asset.id = m.current_asset_id
                WHERE v.source_car_id = ? {auction_filter}
                ORDER BY a.exleasingcar_auction_id, m.media_type,
                         m.source_order, m.id
                """,
                tuple(parameters),
            )
            return [dict(row) for row in rows]

    def upsert_auction_lot(
        self,
        source_car_id: str,
        exleasingcar_auction_id: str | None = None,
        *,
        displayed_lot_reference: str | None = None,
        listing_code_attribute: str | None = None,
        mileage_km: int | None = None,
        sale_type: SaleType | str | None = None,
        collection_eligible: bool | None = True,
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
                collection_eligible=collection_eligible,
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
                    auction_lot_id, highest_offer, currency, observed_at, source
                ) VALUES (?, ?, 'EUR', ?, ?)
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
                SELECT minimum_price, indicative_price, currency
                FROM price_reference_observations
                WHERE auction_lot_id = ? ORDER BY id DESC LIMIT 1
                """,
                (lot_id,),
            ).fetchone()
            new_values = (minimum_value, indicative_value, "EUR")
            if latest is not None and tuple(latest) == new_values:
                return False
            connection.execute(
                """
                INSERT INTO price_reference_observations (
                    auction_lot_id, minimum_price, indicative_price, currency,
                    observed_at, source
                ) VALUES (?, ?, ?, 'EUR', ?, ?)
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
        normalized_currency = (price.currency or "EUR").strip().upper()
        if normalized_currency not in {"EUR", "€", "EURO"}:
            raise ValueError(
                f"Unsupported currency {price.currency!r}; Exleasingcar prices are EUR"
            )
        normalized_currency = "EUR"
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
            new_values = (amount, normalized_currency, price.source_field)
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
                    normalized_currency,
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
        # The countdown belongs to this lot and changes even when the
        # phase and end-time label stay the same.
        new_state = (
            auction_phase,
            bid_status,
            final_call_value,
            live_bidding_value,
            auction_end_at,
            remaining_seconds,
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
                       live_bidding, auction_end_at, remaining_seconds
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

    def save_pdf_field_observations(
        self,
        fields: tuple[PDFExtractedField, ...],
        *,
        auction_lot_id: int | None = None,
    ) -> tuple[int, ...]:
        """Save one report's extracted fields without replacing earlier observations.

        PDFFieldProvenance.lot_id is retained as the source's opaque lot
        identifier. The optional auction_lot_id is a separate, local database
        relationship and must refer to an already existing auction lot. It is
        never inferred from the source identifier.
        """
        if not isinstance(fields, tuple) or any(
            not isinstance(field, PDFExtractedField) for field in fields
        ):
            raise TypeError("fields must be a tuple of PDFExtractedField values")

        if auction_lot_id is not None:
            if isinstance(auction_lot_id, bool) or not isinstance(auction_lot_id, int):
                raise TypeError("auction_lot_id must be an integer or None")
            if auction_lot_id <= 0:
                raise ValueError("auction_lot_id must be a positive integer")

        if not fields:
            return ()

        rows: list[tuple[object, ...]] = []
        report_hashes: set[str] = set()
        for field in fields:
            provenance = field.provenance
            if not isinstance(provenance, PDFFieldProvenance):
                raise TypeError("field provenance must be a PDFFieldProvenance")
            if not isinstance(field.state, PDFFieldState):
                raise TypeError("field state must be a PDFFieldState")

            report_sha256 = provenance.report_sha256
            if not isinstance(report_sha256, str):
                raise TypeError("report_sha256 must be a string")
            if re.fullmatch(r"[0-9a-fA-F]{64}", report_sha256) is None:
                raise ValueError(
                    "report_sha256 must contain exactly 64 hexadecimal characters"
                )
            report_sha256 = report_sha256.lower()
            report_hashes.add(report_sha256)

            for value, name in (
                (field.key, "field key"),
                (provenance.source_label, "source label"),
                (provenance.parser_name, "parser name"),
                (provenance.parser_version, "parser version"),
            ):
                if not isinstance(value, str):
                    raise TypeError(f"{name} must be a string")
                if not value.strip():
                    raise ValueError(f"{name} cannot be empty")

            if provenance.lot_id is not None:
                if not isinstance(provenance.lot_id, str):
                    raise TypeError("source lot_id must be a string or None")
                if not provenance.lot_id.strip():
                    raise ValueError("source lot_id cannot be empty")

            if (
                isinstance(provenance.page_number, bool)
                or not isinstance(provenance.page_number, int)
            ):
                raise TypeError("page_number must be an integer")
            if provenance.page_number <= 0:
                raise ValueError("page_number must be positive")
            if not isinstance(field.raw_text, str):
                raise TypeError("raw_text must be a string")
            if field.normalized_text is not None and not isinstance(
                field.normalized_text, str
            ):
                raise TypeError("normalized_text must be a string or None")
            if not isinstance(provenance.extracted_at, datetime):
                raise TypeError("extracted_at must be a datetime")
            timestamp = self._serialize_datetime(provenance.extracted_at)

            rows.append(
                (
                    report_sha256,
                    provenance.lot_id,
                    auction_lot_id,
                    provenance.page_number,
                    field.key,
                    provenance.source_label,
                    field.raw_text,
                    field.normalized_text,
                    field.state.value,
                    provenance.parser_name,
                    provenance.parser_version,
                    timestamp,
                )
            )

        if len(report_hashes) != 1:
            raise ValueError("all fields in a batch must belong to the same report")

        inserted_ids: list[int] = []
        with self._connect() as connection:
            if auction_lot_id is not None:
                lot = connection.execute(
                    "SELECT id FROM auction_lots WHERE id = ?", (auction_lot_id,)
                ).fetchone()
                if lot is None:
                    raise ValueError(
                        f"auction_lot_id {auction_lot_id} does not refer to an existing lot"
                    )

            for row in rows:
                cursor = connection.execute(
                    """
                    INSERT INTO pdf_field_observations (
                        report_sha256, source_lot_id, auction_lot_id, page_number,
                        field_key, source_label, raw_text, normalized_text, state,
                        parser_name, parser_version, extracted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )
                inserted_ids.append(int(cursor.lastrowid))

        return tuple(inserted_ids)

    def get_pdf_field_observations(
        self,
        *,
        report_sha256: str | None = None,
        auction_lot_id: int | None = None,
    ) -> list[dict[str, object]]:
        """Read observations for a report, a confirmed lot, or their intersection."""
        conditions: list[str] = []
        parameters: list[str | int] = []

        if report_sha256 is not None:
            if not isinstance(report_sha256, str):
                raise TypeError("report_sha256 must be a string or None")
            if re.fullmatch(r"[0-9a-fA-F]{64}", report_sha256) is None:
                raise ValueError(
                    "report_sha256 must contain exactly 64 hexadecimal characters"
                )
            conditions.append("report_sha256 = ?")
            parameters.append(report_sha256.lower())

        if auction_lot_id is not None:
            if isinstance(auction_lot_id, bool) or not isinstance(auction_lot_id, int):
                raise TypeError("auction_lot_id must be an integer or None")
            if auction_lot_id <= 0:
                raise ValueError("auction_lot_id must be a positive integer")
            conditions.append("auction_lot_id = ?")
            parameters.append(auction_lot_id)

        if not conditions:
            raise ValueError("provide report_sha256, auction_lot_id, or both")

        with self._connect() as connection:
            observations = connection.execute(
                f"""
                SELECT id, report_sha256, source_lot_id, auction_lot_id,
                       page_number, field_key, source_label, raw_text,
                       normalized_text, state, parser_name, parser_version,
                       extracted_at
                FROM pdf_field_observations
                WHERE {" AND ".join(conditions)}
                ORDER BY id
                """,
                tuple(parameters),
            ).fetchall()
        return [dict(row) for row in observations]

    def save_pdf_damage_observations(
        self,
        damages: tuple[PDFDamageObservation, ...],
        *,
        auction_lot_id: int | None = None,
    ) -> tuple[int, ...]:
        """Save numbered damage observations without replacing earlier extractions."""
        if not isinstance(damages, tuple) or any(
            not isinstance(damage, PDFDamageObservation) for damage in damages
        ):
            raise TypeError("damages must be a tuple of PDFDamageObservation values")

        if auction_lot_id is not None:
            if isinstance(auction_lot_id, bool) or not isinstance(auction_lot_id, int):
                raise TypeError("auction_lot_id must be an integer or None")
            if auction_lot_id <= 0:
                raise ValueError("auction_lot_id must be a positive integer")

        if not damages:
            return ()

        rows: list[tuple[object, ...]] = []
        report_hashes: set[str] = set()
        for damage in damages:
            provenance = damage.provenance
            if not isinstance(provenance, PDFFieldProvenance):
                raise TypeError("damage provenance must be a PDFFieldProvenance")

            report_sha256 = provenance.report_sha256
            if not isinstance(report_sha256, str):
                raise TypeError("report_sha256 must be a string")
            if re.fullmatch(r"[0-9a-fA-F]{64}", report_sha256) is None:
                raise ValueError(
                    "report_sha256 must contain exactly 64 hexadecimal characters"
                )
            report_sha256 = report_sha256.lower()
            report_hashes.add(report_sha256)

            for value, name in (
                (damage.category, "damage category"),
                (damage.component, "damage component"),
                (damage.description, "damage description"),
                (provenance.source_label, "source label"),
                (provenance.parser_name, "parser name"),
                (provenance.parser_version, "parser version"),
            ):
                if not isinstance(value, str):
                    raise TypeError(f"{name} must be a string")
                if not value.strip():
                    raise ValueError(f"{name} cannot be empty")

            if provenance.lot_id is not None:
                if not isinstance(provenance.lot_id, str):
                    raise TypeError("source lot_id must be a string or None")
                if not provenance.lot_id.strip():
                    raise ValueError("source lot_id cannot be empty")

            for value, name in (
                (damage.item_number, "item_number"),
                (provenance.page_number, "page_number"),
            ):
                if isinstance(value, bool) or not isinstance(value, int):
                    raise TypeError(f"{name} must be an integer")
                if value <= 0:
                    raise ValueError(f"{name} must be positive")

            if not isinstance(provenance.extracted_at, datetime):
                raise TypeError("extracted_at must be a datetime")
            timestamp = self._serialize_datetime(provenance.extracted_at)

            photo = damage.photo
            if photo is None:
                photo_values: tuple[object, ...] = (None,) * 7
            else:
                if not isinstance(photo, PDFDamagePhotoReference):
                    raise TypeError("photo must be a PDFDamagePhotoReference or None")
                if (
                    isinstance(photo.page_number, bool)
                    or not isinstance(photo.page_number, int)
                    or photo.page_number <= 0
                ):
                    raise ValueError("photo page_number must be a positive integer")
                if (
                    isinstance(photo.image_index, bool)
                    or not isinstance(photo.image_index, int)
                    or photo.image_index <= 0
                ):
                    raise ValueError("photo image_index must be a positive integer")
                if not isinstance(photo.caption, str) or not photo.caption.strip():
                    raise ValueError("photo caption cannot be empty")

                coordinates = (photo.x0, photo.top, photo.x1, photo.bottom)
                if any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    for value in coordinates
                ):
                    raise TypeError("photo coordinates must be finite numbers")
                if photo.x1 <= photo.x0 or photo.bottom <= photo.top:
                    raise ValueError("photo rectangle must have positive width and height")
                photo_values = (
                    photo.page_number,
                    photo.image_index,
                    photo.caption,
                    float(photo.x0),
                    float(photo.top),
                    float(photo.x1),
                    float(photo.bottom),
                )

            rows.append(
                (
                    report_sha256,
                    provenance.lot_id,
                    auction_lot_id,
                    damage.item_number,
                    damage.category,
                    damage.component,
                    damage.description,
                    provenance.page_number,
                    *photo_values,
                    provenance.parser_name,
                    provenance.parser_version,
                    timestamp,
                )
            )

        if len(report_hashes) != 1:
            raise ValueError("all damage observations in a batch must belong to the same report")

        inserted_ids: list[int] = []
        with self._connect() as connection:
            if auction_lot_id is not None:
                lot = connection.execute(
                    "SELECT id FROM auction_lots WHERE id = ?", (auction_lot_id,)
                ).fetchone()
                if lot is None:
                    raise ValueError(
                        f"auction_lot_id {auction_lot_id} does not refer to an existing lot"
                    )

            for row in rows:
                cursor = connection.execute(
                    """
                    INSERT INTO pdf_damage_observations (
                        report_sha256, source_lot_id, auction_lot_id, item_number,
                        category, component, damage_description, page_number,
                        photo_page_number, photo_index, photo_caption,
                        photo_x0, photo_top, photo_x1, photo_bottom,
                        parser_name, parser_version, extracted_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    row,
                )
                inserted_ids.append(int(cursor.lastrowid))
        return tuple(inserted_ids)

    def get_pdf_damage_observations(
        self,
        *,
        report_sha256: str | None = None,
        auction_lot_id: int | None = None,
    ) -> list[dict[str, object]]:
        """Read damage observations for a report, a confirmed lot, or both."""
        conditions: list[str] = []
        parameters: list[str | int] = []

        if report_sha256 is not None:
            if not isinstance(report_sha256, str):
                raise TypeError("report_sha256 must be a string or None")
            if re.fullmatch(r"[0-9a-fA-F]{64}", report_sha256) is None:
                raise ValueError(
                    "report_sha256 must contain exactly 64 hexadecimal characters"
                )
            conditions.append("report_sha256 = ?")
            parameters.append(report_sha256.lower())

        if auction_lot_id is not None:
            if isinstance(auction_lot_id, bool) or not isinstance(auction_lot_id, int):
                raise TypeError("auction_lot_id must be an integer or None")
            if auction_lot_id <= 0:
                raise ValueError("auction_lot_id must be a positive integer")
            conditions.append("auction_lot_id = ?")
            parameters.append(auction_lot_id)

        if not conditions:
            raise ValueError("provide report_sha256, auction_lot_id, or both")

        with self._connect() as connection:
            observations = connection.execute(
                f"""
                SELECT id, report_sha256, source_lot_id, auction_lot_id,
                       item_number, category, component, damage_description,
                       page_number, photo_page_number, photo_index, photo_caption,
                       photo_x0, photo_top, photo_x1, photo_bottom,
                       parser_name, parser_version, extracted_at
                FROM pdf_damage_observations
                WHERE {" AND ".join(conditions)}
                ORDER BY id
                """,
                tuple(parameters),
            ).fetchall()
        return [dict(row) for row in observations]
