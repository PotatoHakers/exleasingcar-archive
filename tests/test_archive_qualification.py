from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from exleasingcar_archive.api_parser import parse_car_update_response
from exleasingcar_archive.collector import CapturedApiResponse, ExleasingcarCollector
from exleasingcar_archive.database import SCHEMA_VERSION, ArchiveDatabase
from exleasingcar_archive.detail_parser import parse_vehicle_detail
from exleasingcar_archive.ingestion import AuctionContext, LotMetadata
from exleasingcar_archive.models import PriceType, SaleType, VehicleStatus
from exleasingcar_archive.parsers import ParsedLot, PriceObservation


class FakePage:
    def __init__(self, car_id: str, html: str) -> None:
        self.url = f"https://www.exleasingcar.com/ru/auto-details/{car_id}"
        self.html = html

    def goto(self, url: str, **kwargs: object) -> None:
        self.url = url

    def content(self) -> str:
        return self.html

    def title(self) -> str:
        return "Example vehicle"


def detail_html(car_id: str, bidding_classes: str, *, fixed_price: str | None = None) -> str:
    fixed = (
        f'<div class="fixed-price"><input class="buy-offer" '
        f'value="Купить сейчас за € {fixed_price}"></div>'
        if fixed_price is not None
        else ""
    )
    return f"""
    <h1>Example vehicle</h1>
    <div class="auto-block" car-id="{car_id}">
      <div class="bids active {bidding_classes} bids_{car_id}">
        <div class="current-price">Текущая цена: € 8 500</div>
      </div>
      {fixed}
      <div id="gallery-area">
        <div class="gallery-thumbs">
          <a onclick="change_thumb(this, 'https://data.exleasingcar.com/{car_id}.jpg')">
            <img src="https://data.exleasingcar.com/{car_id}-thumb.jpg">
          </a>
        </div>
      </div>
    </div>
    """


def seed_candidate(database: ArchiveDatabase, car_id: str, auction_id: str) -> None:
    database.upsert_auction(auction_id)
    database.upsert_vehicle(car_id, displayed_title="Example vehicle")
    database.upsert_auction_lot(
        car_id,
        auction_id,
        collection_eligible=None,
    )


def test_open_auction_is_archived_before_trading_starts() -> None:
    result = parse_vehicle_detail(
        """
        <div class="auto-block" car-id="car-open">
          <div class="bids active open bids_car-open">
            <div class="current-price">Начальная цена: € 7 500</div>
            <input class="my_bid_car-open" value="0">
          </div>
        </div>
        """
    )

    assert result.sale_type is SaleType.OPEN_AUCTION
    assert result.should_monitor is True
    assert result.should_archive is True
    assert result.displayed_auction_price is not None
    assert result.displayed_auction_price.amount == Decimal(7500)


def test_closed_hidden_bid_auction_without_buy_now_is_not_archived() -> None:
    result = parse_vehicle_detail(
        """
        <div class="auto-block" car-id="car-hidden">
          <div class="bids active closed bids_car-hidden">
            <input class="bid_car-hidden" value="0">
          </div>
        </div>
        """
    )

    assert result.sale_type is SaleType.CLOSED_AUCTION
    assert result.should_monitor is False
    assert result.should_archive is False
    assert result.fixed_price is None


def test_closed_auction_with_public_buy_now_price_is_archived() -> None:
    result = parse_vehicle_detail(
        """
        <div class="auto-block" car-id="car-closed-fixed">
          <div class="bids active closed fix bids_car-closed-fixed"></div>
          <div class="fixed-price">
            <input class="buy-offer" value="Купить сейчас за € 8 900">
          </div>
        </div>
        """
    )

    assert result.sale_type is SaleType.CLOSED_AUCTION
    assert result.has_fixed_price_option is True
    assert result.fixed_price is not None
    assert result.fixed_price.amount == Decimal(8900)
    assert result.should_archive is True


def test_open_auction_can_keep_public_start_price_and_buy_now_separate() -> None:
    result = parse_vehicle_detail(
        """
        <div class="auto-block" car-id="car-open-fixed">
          <div class="bids active open fix bids_car-open-fixed">
            <div class="current-price">Текущая цена: € 8 500</div>
          </div>
          <div class="fixed-price">
            <input class="buy-offer" value="Купить сейчас за € 9 200">
          </div>
        </div>
        """
    )

    assert result.sale_type is SaleType.OPEN_AUCTION
    assert result.should_monitor is True
    assert result.should_archive is True
    assert result.displayed_auction_price is not None
    assert result.displayed_auction_price.amount == Decimal(8500)
    assert result.fixed_price is not None
    assert result.fixed_price.amount == Decimal(9200)


def test_collector_excludes_closed_lot_before_detail_and_media_persistence(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "exleasingcar_archive.collector._wait_for_dynamic_content",
        lambda page: None,
    )
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    auction_id = "auction-qualification"
    seed_candidate(database, "car-hidden", auction_id)
    collector = ExleasingcarCollector(database, tmp_path / "raw", page_delay_seconds=0)

    outcome = collector._enrich_vehicle_detail(
        AuctionContext(auction_id),
        "car-hidden",
        "https://www.exleasingcar.com/ru/auto-details/car-hidden",
        FakePage("car-hidden", detail_html("car-hidden", "closed")),
        datetime.now(timezone.utc),
        tmp_path / "snapshot",
    )

    with sqlite3.connect(database.path) as connection:
        lot = connection.execute(
            """
            SELECT sale_type, collection_eligible, has_fixed_price,
                   last_detail_checked_at
            FROM auction_lots
            """
        ).fetchone()
        media_count = connection.execute(
            "SELECT COUNT(*) FROM lot_media_sources"
        ).fetchone()[0]
        detail_price_count = connection.execute(
            "SELECT COUNT(*) FROM lot_price_observations"
        ).fetchone()[0]
        vehicle = connection.execute(
            "SELECT location_text, chassis_number FROM vehicles"
        ).fetchone()

    assert outcome == "excluded_closed"
    assert lot[0] == "closed_auction"
    assert lot[1:3] == (0, 0)
    assert lot[3] is not None
    assert media_count == 0
    assert detail_price_count == 0
    assert vehicle == (None, None)


def test_collector_archives_closed_lot_when_buy_now_price_is_verified(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "exleasingcar_archive.collector._wait_for_dynamic_content",
        lambda page: None,
    )
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    auction_id = "auction-qualification"
    seed_candidate(database, "car-closed-fixed", auction_id)
    collector = ExleasingcarCollector(database, tmp_path / "raw", page_delay_seconds=0)

    outcome = collector._enrich_vehicle_detail(
        AuctionContext(auction_id),
        "car-closed-fixed",
        "https://www.exleasingcar.com/ru/auto-details/car-closed-fixed",
        FakePage(
            "car-closed-fixed",
            detail_html("car-closed-fixed", "closed fix", fixed_price="8 900"),
        ),
        datetime.now(timezone.utc),
        tmp_path / "snapshot",
    )

    with sqlite3.connect(database.path) as connection:
        lot = connection.execute(
            "SELECT sale_type, collection_eligible, has_fixed_price FROM auction_lots"
        ).fetchone()
        fixed_price = connection.execute(
            "SELECT price_type, amount FROM lot_price_observations"
        ).fetchone()
        media_count = connection.execute(
            "SELECT COUNT(*) FROM lot_media_sources WHERE media_type = 'photo'"
        ).fetchone()[0]

    assert outcome == "fixed_price"
    assert lot == ("closed_auction", 1, 1)
    assert fixed_price == ("fixed_price", "8900")
    assert media_count == 1


def test_detail_check_order_revalidates_all_modes_weekly(tmp_path) -> None:
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    auction_id = "auction-check-order"
    database.upsert_auction(auction_id)
    for car_id in ("car-open", "car-closed-recent", "car-closed-old"):
        seed_candidate(database, car_id, auction_id)

    now = datetime.now(timezone.utc)
    database.set_auction_lot_qualification(
        "car-open", auction_id, sale_type=SaleType.OPEN_AUCTION,
        has_fixed_price=False, collection_eligible=True, checked_at=now,
    )
    database.set_auction_lot_qualification(
        "car-closed-recent", auction_id, sale_type=SaleType.CLOSED_AUCTION,
        has_fixed_price=False, collection_eligible=False, checked_at=now,
    )
    database.set_auction_lot_qualification(
        "car-closed-old", auction_id, sale_type=SaleType.CLOSED_AUCTION,
        has_fixed_price=False, collection_eligible=False,
        checked_at=now - timedelta(days=8),
    )

    selected = set(
        database.get_detail_check_order(
            auction_id,
            {"car-open", "car-closed-recent", "car-closed-old"},
        )
    )

    assert selected == {"car-closed-old"}


def test_api_bid_history_is_written_only_for_open_auction_lots(tmp_path) -> None:
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    auction_id = "auction-api-filter"
    for car_id in ("car-open", "car-fixed", "car-hidden"):
        seed_candidate(database, car_id, auction_id)
    database.set_auction_lot_qualification(
        "car-open", auction_id, sale_type=SaleType.OPEN_AUCTION,
        has_fixed_price=False, collection_eligible=True,
    )
    database.set_auction_lot_qualification(
        "car-fixed", auction_id, sale_type=SaleType.CLOSED_AUCTION,
        has_fixed_price=True, collection_eligible=True,
    )
    database.set_auction_lot_qualification(
        "car-hidden", auction_id, sale_type=SaleType.CLOSED_AUCTION,
        has_fixed_price=False, collection_eligible=False,
    )

    payload = {
        car_id: {
            "highest_offer": "12000",
            "text_minimali_kaina": "9000",
            "text_orientacine_kaina": "10000",
            "bid_status": "leading",
            "car_auction_time": {
                "type": "final-call",
                "image": "final-call-active",
                "endDate": "2026-11-23 12:00:00",
                "remainingSeconds": 30,
            },
        }
        for car_id in ("car-open", "car-fixed", "car-hidden")
    }
    observed_at = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)
    collector = ExleasingcarCollector(database, tmp_path / "raw", page_delay_seconds=0)
    collector.recorder.responses.append(
        CapturedApiResponse(
            "https://www.exleasingcar.com/ajax_update_car_form.v3.1.php",
            payload,
            observed_at,
        )
    )

    collector._persist_api_updates(
        AuctionContext(auction_id),
        {"car-open", "car-fixed"},
        {"car-open": LotMetadata(), "car-fixed": LotMetadata()},
        open_auction_car_ids={"car-open"},
    )

    with sqlite3.connect(database.path) as connection:
        bid_rows = connection.execute(
            """
            SELECT v.source_car_id, p.highest_offer
            FROM price_observations AS p
            JOIN auction_lots AS l ON l.id = p.auction_lot_id
            JOIN vehicles AS v ON v.id = l.vehicle_id
            ORDER BY v.source_car_id
            """
        ).fetchall()
        event_count = connection.execute(
            "SELECT COUNT(*) FROM auction_events"
        ).fetchone()[0]
        reference_count = connection.execute(
            "SELECT COUNT(*) FROM price_reference_observations"
        ).fetchone()[0]

    assert bid_rows == [("car-open", "12000")]
    assert event_count == 1
    assert reference_count == 2


def test_r2_media_queue_ignores_sources_of_excluded_lots(tmp_path) -> None:
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    auction_id = "auction-media-filter"
    for car_id in ("car-open", "car-hidden"):
        seed_candidate(database, car_id, auction_id)
    database.set_auction_lot_qualification(
        "car-open", auction_id, sale_type=SaleType.OPEN_AUCTION,
        has_fixed_price=False, collection_eligible=True,
    )
    database.set_auction_lot_qualification(
        "car-hidden", auction_id, sale_type=SaleType.CLOSED_AUCTION,
        has_fixed_price=False, collection_eligible=False,
    )
    database.upsert_lot_media_source(
        "car-open", auction_id, "photo", "https://data.exleasingcar.com/open.jpg"
    )
    database.upsert_lot_media_source(
        "car-hidden", auction_id, "photo", "https://data.exleasingcar.com/hidden.jpg"
    )

    pending = database.get_pending_media_sources(10)

    assert len(pending) == 1
    assert pending[0]["source_url"] == "https://data.exleasingcar.com/open.jpg"


def test_v13_migration_backfills_fixed_price_eligibility() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE auction_lots (id INTEGER PRIMARY KEY, sale_type TEXT)"
    )
    connection.execute(
        "CREATE TABLE lot_price_observations (auction_lot_id INTEGER, price_type TEXT)"
    )
    connection.executemany(
        "INSERT INTO auction_lots (id, sale_type) VALUES (?, ?)",
        [
            (1, "open_auction"),
            (2, "closed_auction"),
            (3, "closed_auction"),
            (4, "fixed_price"),
            (5, None),
        ],
    )
    connection.execute(
        "INSERT INTO lot_price_observations VALUES (3, 'fixed_price')"
    )

    ArchiveDatabase._migrate_v13_to_v14(connection)
    rows = [
        tuple(row)
        for row in connection.execute(
            "SELECT id, collection_eligible, has_fixed_price FROM auction_lots ORDER BY id"
        ).fetchall()
    ]
    connection.close()

    assert SCHEMA_VERSION == 15
    assert rows == [
        (1, 1, 0),
        (2, 0, 0),
        (3, 1, 1),
        (4, 0, 0),
        (5, 0, 0),
    ]


def test_catalog_refresh_preserves_a_previously_qualified_lot(tmp_path) -> None:
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    database.upsert_auction("auction-preserve")
    database.upsert_vehicle("car-preserve")
    database.upsert_auction_lot(
        "car-preserve", "auction-preserve", collection_eligible=None
    )
    assert database.set_auction_lot_qualification(
        "car-preserve",
        "auction-preserve",
        sale_type=SaleType.OPEN_AUCTION,
        has_fixed_price=False,
        collection_eligible=True,
    )

    # The catalogue pass updates identifiers/mileage but has no new decision
    # about eligibility, so None must preserve the last detail-page result.
    database.upsert_auction_lot(
        "car-preserve",
        "auction-preserve",
        mileage_km=12345,
        collection_eligible=None,
    )

    with sqlite3.connect(database.path) as connection:
        row = connection.execute(
            """
            SELECT collection_eligible, sale_type, mileage_km
            FROM auction_lots
            """
        ).fetchone()

    assert row == (1, "open_auction", 12345)


def test_failed_detail_attempts_are_not_retried_before_weekly_recheck(tmp_path) -> None:
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    auction_id = "auction-failed-attempt"
    database.upsert_auction(auction_id)
    for car_id in ("car-failed", "car-open"):
        database.upsert_vehicle(car_id)
        database.upsert_auction_lot(
            car_id, auction_id, collection_eligible=None
        )

    now = datetime.now(timezone.utc)
    database.set_auction_lot_qualification(
        "car-open",
        auction_id,
        sale_type=SaleType.OPEN_AUCTION,
        has_fixed_price=False,
        collection_eligible=True,
        checked_at=now,
    )
    database.mark_detail_attempted(
        "car-failed",
        auction_id,
        attempted_at=now,
    )

    selected = set(
        database.get_detail_check_order(
            auction_id,
            {"car-failed", "car-open"},
        )
    )

    assert selected == set()


def test_api_price_insert_does_not_promote_an_unqualified_candidate(tmp_path) -> None:
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    database.upsert_auction("auction-candidate")
    database.upsert_vehicle("car-candidate")
    database.upsert_auction_lot(
        "car-candidate", "auction-candidate", collection_eligible=None
    )

    database.record_price_observation(
        "car-candidate",
        Decimal(12345),
        exleasingcar_auction_id="auction-candidate",
    )

    with sqlite3.connect(database.path) as connection:
        eligibility, sale_type = connection.execute(
            "SELECT collection_eligible, sale_type FROM auction_lots"
        ).fetchone()

    assert eligibility == 0
    assert sale_type is None


def test_zero_highest_offer_is_preserved_but_not_a_bid_and_terminal_api_state_is_explicit():
    parsed = parse_car_update_response(
        {
            "car-zero": {
                "highest_offer": 0,
                "car_auction_time": {
                    "type": "",
                    "liveBidding": "0",
                    "remainingSeconds": 0,
                },
            },
            "car-positive": {
                "highest_offer": 12500,
                "car_auction_time": {
                    "type": "ongoing",
                    "liveBidding": "1",
                    "remainingSeconds": 180,
                },
            },
            "car-final-call": {
                "highest_offer": 15000,
                "car_auction_time": {
                    "type": "final-call",
                    "image": "final-call-active",
                    "liveBidding": "0",
                    "remainingSeconds": 0,
                },
            },
        }
    )

    zero = parsed["car-zero"]
    positive = parsed["car-positive"]
    final_call = parsed["car-final-call"]

    assert zero.highest_offer == Decimal(0)
    assert zero.has_positive_highest_offer is False
    assert zero.completion_reason == "countdown_expired"
    assert positive.has_positive_highest_offer is True
    assert positive.completion_reason is None
    # A zero timer alone cannot finish an auction while FINAL CALL is active.
    assert final_call.completion_reason is None


def test_explicit_terminal_api_status_completes_even_without_a_countdown():
    parsed = parse_car_update_response(
        {
            "car-finished": {
                "highest_offer": 12500,
                "bid_status": "completed",
                "car_auction_time": {
                    "liveBidding": "0",
                },
            }
        }
    )

    assert parsed["car-finished"].completion_reason == "api_terminal_status"


def test_completed_open_auction_keeps_final_observation_and_stops_later_updates(
    tmp_path,
) -> None:
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    auction_id = "auction-completes"
    seed_candidate(database, "car-completes", auction_id)
    database.set_auction_lot_qualification(
        "car-completes",
        auction_id,
        sale_type=SaleType.OPEN_AUCTION,
        has_fixed_price=False,
        collection_eligible=True,
    )
    collector = ExleasingcarCollector(database, tmp_path / "raw", page_delay_seconds=0)
    first_seen = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)
    completed_at = datetime(2026, 11, 22, 10, 5, tzinfo=timezone.utc)
    late_seen = datetime(2026, 11, 22, 10, 10, tzinfo=timezone.utc)

    collector.recorder.responses.extend(
        (
            CapturedApiResponse(
                "https://www.exleasingcar.com/ajax_update_car_form.v3.1.php",
                {
                    "car-completes": {
                        "highest_offer": 12000,
                        "car_auction_time": {
                            "type": "ongoing",
                            "liveBidding": "1",
                            "remainingSeconds": 300,
                        },
                    }
                },
                first_seen,
            ),
            CapturedApiResponse(
                "https://www.exleasingcar.com/ajax_update_car_form.v3.1.php",
                {
                    "car-completes": {
                        "highest_offer": 0,
                        "car_auction_time": {
                            "type": "",
                            "liveBidding": "0",
                            "remainingSeconds": 0,
                        },
                    }
                },
                completed_at,
            ),
            CapturedApiResponse(
                "https://www.exleasingcar.com/ajax_update_car_form.v3.1.php",
                {
                    "car-completes": {
                        "highest_offer": 15000,
                        "car_auction_time": {
                            "type": "ongoing",
                            "liveBidding": "1",
                            "remainingSeconds": 120,
                        },
                    }
                },
                late_seen,
            ),
        )
    )

    collector._persist_api_updates(
        AuctionContext(auction_id),
        {"car-completes"},
        {"car-completes": LotMetadata()},
        open_auction_car_ids={"car-completes"},
    )

    with sqlite3.connect(database.path) as connection:
        price_values = [
            row[0]
            for row in connection.execute(
                """
                SELECT highest_offer FROM price_observations
                ORDER BY observed_at, id
                """
            )
        ]
        events = connection.execute(
            """
            SELECT remaining_seconds, live_bidding
            FROM auction_events ORDER BY observed_at, id
            """
        ).fetchall()
        completion = connection.execute(
            """
            SELECT monitoring_completed_at, monitoring_completion_reason
            FROM auction_lots
            """
        ).fetchone()

    assert price_values == ["12000", "0"]
    assert events == [(300, 1), (0, 0)]
    expected_completed_at = completed_at.isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
    assert completion == (expected_completed_at, "countdown_expired")
    assert database.get_completed_open_auction_lots(
        auction_id, {"car-completes"}
    ) == {"car-completes"}
    assert database.get_detail_check_order(
        auction_id, {"car-completes"}
    ) == []

    # A later collector invocation does not add another value for a completed lot.
    later_collector = ExleasingcarCollector(
        database, tmp_path / "raw", page_delay_seconds=0
    )
    later_collector.recorder.responses.append(
        CapturedApiResponse(
            "https://www.exleasingcar.com/ajax_update_car_form.v3.1.php",
            {
                "car-completes": {
                    "highest_offer": 18000,
                    "car_auction_time": {
                        "type": "ongoing",
                        "liveBidding": "1",
                        "remainingSeconds": 60,
                    },
                }
            },
            late_seen,
        )
    )
    later_collector._persist_api_updates(
        AuctionContext(auction_id),
        {"car-completes"},
        {"car-completes": LotMetadata()},
        open_auction_car_ids={"car-completes"},
    )

    with sqlite3.connect(database.path) as connection:
        after = connection.execute(
            "SELECT highest_offer FROM price_observations ORDER BY id"
        ).fetchall()
    assert after == [("12000",), ("0",)]


def test_v14_migration_marks_only_conclusively_finished_open_auctions():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE auction_lots (
            id INTEGER PRIMARY KEY,
            sale_type TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE auction_events (
            id INTEGER PRIMARY KEY,
            auction_lot_id INTEGER NOT NULL,
            remaining_seconds INTEGER,
            live_bidding INTEGER,
            final_call_active INTEGER,
            observed_at TEXT NOT NULL
        )
        """
    )
    connection.executemany(
        "INSERT INTO auction_lots (id, sale_type) VALUES (?, ?)",
        [
            (1, "open_auction"),
            (2, "open_auction"),
            (3, "closed_auction"),
            (4, "open_auction"),
        ],
    )
    connection.executemany(
        """
        INSERT INTO auction_events (
            auction_lot_id, remaining_seconds, live_bidding,
            final_call_active, observed_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            (1, 0, 0, 0, "2026-11-22T10:00:00Z"),
            (2, 0, 0, 1, "2026-11-22T10:00:00Z"),
            (3, 0, 0, 0, "2026-11-22T10:00:00Z"),
            (4, 0, 0, 0, "2026-11-22T10:00:00Z"),
            (4, 30, 1, 0, "2026-11-22T10:01:00Z"),
        ],
    )

    ArchiveDatabase._migrate_v14_to_v15(connection)
    rows = [
        tuple(row)
        for row in connection.execute(
            """
            SELECT id, monitoring_completed_at, monitoring_completion_reason
            FROM auction_lots ORDER BY id
            """
        ).fetchall()
    ]
    connection.close()

    assert SCHEMA_VERSION == 15
    assert rows == [
        (1, "2026-11-22T10:00:00Z", "countdown_expired"),
        (2, None, None),
        (3, None, None),
        (4, None, None),
    ]


def test_live_bidding_is_tracked_per_lot_in_the_same_auction(tmp_path) -> None:
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    auction_id = "auction-two-lots"
    for car_id in ("car-ended", "car-active"):
        seed_candidate(database, car_id, auction_id)
        database.set_auction_lot_qualification(
            car_id,
            auction_id,
            sale_type=SaleType.OPEN_AUCTION,
            has_fixed_price=False,
            collection_eligible=True,
        )

    collector = ExleasingcarCollector(database, tmp_path / "raw", page_delay_seconds=0)
    first_seen = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
    second_seen = datetime(2026, 11, 23, 10, 5, tzinfo=timezone.utc)
    collector.recorder.responses.extend(
        (
            CapturedApiResponse(
                "https://www.exleasingcar.com/ajax_update_car_form.v3.1.php",
                {
                    "car-ended": {
                        "car_auction_time": {
                            "liveBidding": "0",
                            "remainingSeconds": 0,
                        }
                    },
                    "car-active": {
                        "highest_offer": 13000,
                        "car_auction_time": {
                            "type": "ongoing",
                            "liveBidding": "1",
                            "endDate": "2026-11-23 10:10:00",
                            "remainingSeconds": 240,
                        },
                    },
                },
                first_seen,
            ),
            CapturedApiResponse(
                "https://www.exleasingcar.com/ajax_update_car_form.v3.1.php",
                {
                    "car-ended": {
                        "highest_offer": 9000,
                        "car_auction_time": {
                            "liveBidding": "0",
                            "remainingSeconds": 0,
                        },
                    },
                    "car-active": {
                        "highest_offer": 14500,
                        "car_auction_time": {
                            "type": "ongoing",
                            "liveBidding": "1",
                            "endDate": "2026-11-23 10:10:00",
                            "remainingSeconds": 180,
                        },
                    },
                },
                second_seen,
            ),
        )
    )

    collector._persist_api_updates(
        AuctionContext(auction_id),
        {"car-ended", "car-active"},
        {"car-ended": LotMetadata(), "car-active": LotMetadata()},
        open_auction_car_ids={"car-ended", "car-active"},
    )

    with sqlite3.connect(database.path) as connection:
        prices = connection.execute(
            """
            SELECT v.source_car_id, p.highest_offer
            FROM price_observations AS p
            JOIN auction_lots AS l ON l.id = p.auction_lot_id
            JOIN vehicles AS v ON v.id = l.vehicle_id
            ORDER BY v.source_car_id, p.id
            """
        ).fetchall()
        countdowns = connection.execute(
            """
            SELECT v.source_car_id, e.remaining_seconds
            FROM auction_events AS e
            JOIN auction_lots AS l ON l.id = e.auction_lot_id
            JOIN vehicles AS v ON v.id = l.vehicle_id
            ORDER BY v.source_car_id, e.id
            """
        ).fetchall()
        completion = connection.execute(
            """
            SELECT v.source_car_id, l.monitoring_completion_reason
            FROM auction_lots AS l
            JOIN vehicles AS v ON v.id = l.vehicle_id
            ORDER BY v.source_car_id
            """
        ).fetchall()

    assert prices == [("car-active", "13000"), ("car-active", "14500")]
    assert countdowns == [
        ("car-active", 240),
        ("car-active", 180),
        ("car-ended", 0),
    ]
    assert completion == [
        ("car-active", None),
        ("car-ended", "countdown_expired"),
    ]
    assert database.get_completed_open_auction_lots(
        auction_id, {"car-ended", "car-active"}
    ) == {"car-ended"}


def test_completed_catalogue_lot_keeps_its_displayed_price_without_a_bid_or_timer(
    tmp_path,
) -> None:
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    auction_id = "auction-no-bid-finished"
    seed_candidate(database, "car-no-bid", auction_id)
    database.set_auction_lot_qualification(
        "car-no-bid",
        auction_id,
        sale_type=SaleType.OPEN_AUCTION,
        has_fixed_price=False,
        collection_eligible=True,
    )
    collector = ExleasingcarCollector(database, tmp_path / "raw", page_delay_seconds=0)
    observed_at = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
    catalogue_lot = ParsedLot(
        source_car_id="car-no-bid",
        title="Example vehicle",
        source_url=None,
        status=VehicleStatus.COMPLETED,
        raw_status="Аукцион закончился!",
        is_hot_bid=False,
        current_price=PriceObservation(
            price_type=PriceType.CURRENT_PRICE,
            amount=Decimal(7000),
            currency="EUR",
            raw_text="7 000 EUR",
            source_field="maximum_bid_car-no-bid",
        ),
    )

    collector._persist_catalog_lot_observations(
        AuctionContext(auction_id),
        {"car-no-bid": (catalogue_lot, observed_at)},
        {"car-no-bid"},
        completed_before_run=set(),
    )

    with sqlite3.connect(database.path) as connection:
        price = connection.execute(
            "SELECT price_type, amount, source FROM lot_price_observations"
        ).fetchone()
        completion = connection.execute(
            """
            SELECT monitoring_completion_reason
            FROM auction_lots
            """
        ).fetchone()[0]
        event = connection.execute(
            "SELECT bid_status, source FROM auction_events"
        ).fetchone()

    assert price == ("displayed_auction_price", "7000", "auction_catalog_html")
    assert completion == "catalog_terminal_status"
    assert event == ("completed", "auction_catalog_html")


def test_single_public_bid_without_a_timer_stays_active(tmp_path) -> None:
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    auction_id = "auction-one-bid-no-timer"
    seed_candidate(database, "car-one-bid", auction_id)
    database.set_auction_lot_qualification(
        "car-one-bid",
        auction_id,
        sale_type=SaleType.OPEN_AUCTION,
        has_fixed_price=False,
        collection_eligible=True,
    )
    collector = ExleasingcarCollector(database, tmp_path / "raw", page_delay_seconds=0)
    collector.recorder.responses.append(
        CapturedApiResponse(
            "https://www.exleasingcar.com/ajax_update_car_form.v3.1.php",
            {"car-one-bid": {"highest_offer": "8200"}},
            datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc),
        )
    )

    collector._persist_api_updates(
        AuctionContext(auction_id),
        {"car-one-bid"},
        {"car-one-bid": LotMetadata()},
        open_auction_car_ids={"car-one-bid"},
    )

    with sqlite3.connect(database.path) as connection:
        price = connection.execute(
            "SELECT highest_offer FROM price_observations"
        ).fetchone()[0]
        completion = connection.execute(
            "SELECT monitoring_completed_at FROM auction_lots"
        ).fetchone()[0]
        event_count = connection.execute(
            "SELECT COUNT(*) FROM auction_events"
        ).fetchone()[0]

    assert price == "8200"
    assert completion is None
    assert event_count == 0
