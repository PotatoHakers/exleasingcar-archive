"""Bridge parsed Exleasingcar API updates to the SQLite archive."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from .api_parser import ParsedCarUpdate
from .database import ArchiveDatabase


@dataclass(frozen=True, slots=True)
class AuctionContext:
    """Known metadata for one Exleasingcar auction."""

    exleasingcar_auction_id: str
    source_auction_name: str | None = None
    source_auction_country_code: str | None = None
    source_auction_country_name: str | None = None
    auction_type_label: str | None = None
    auction_status_label: str | None = None


@dataclass(frozen=True, slots=True)
class LotMetadata:
    """Optional identifiers visible in the auction catalogue."""

    displayed_lot_reference: str | None = None
    listing_code_attribute: str | None = None


@dataclass(frozen=True, slots=True)
class IngestionSummary:
    """Counts of records written during one API response ingestion."""

    cars_processed: int
    lots_upserted: int
    prices_inserted: int
    events_inserted: int


def store_car_updates(
    database: ArchiveDatabase,
    updates: Mapping[str, ParsedCarUpdate],
    auction: AuctionContext,
    *,
    observed_at: datetime | None = None,
    lot_metadata: Mapping[str, LotMetadata] | None = None,
) -> IngestionSummary:
    """Store one parsed API response against a known Exleasingcar auction.

    The API response identifies cars and their changing auction data, but does
    not reliably identify the enclosing Exleasingcar auction. That context is
    therefore supplied separately from the parsed auction catalogue page.
    """
    if not isinstance(database, ArchiveDatabase):
        raise TypeError("database must be an ArchiveDatabase")

    if not isinstance(auction, AuctionContext):
        raise TypeError("auction must be an AuctionContext")

    timestamp = observed_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")

    metadata_by_car = lot_metadata or {}
    validated_updates: list[tuple[str, ParsedCarUpdate, LotMetadata]] = []

    # Validate the complete payload before writing anything to the database.
    for response_car_id, car in updates.items():
        if not isinstance(car, ParsedCarUpdate):
            raise TypeError(
                f"Expected ParsedCarUpdate for car {response_car_id}"
            )

        if str(response_car_id) != car.source_car_id:
            raise ValueError(
                "Response key does not match ParsedCarUpdate.source_car_id: "
                f"{response_car_id!r} != {car.source_car_id!r}"
            )

        lot = metadata_by_car.get(car.source_car_id, LotMetadata())
        if not isinstance(lot, LotMetadata):
            raise TypeError(
                f"Lot metadata for car {car.source_car_id} must be LotMetadata"
            )
        validated_updates.append((car.source_car_id, car, lot))

    database.upsert_auction(
        auction.exleasingcar_auction_id,
        source_auction_name=auction.source_auction_name,
        source_auction_country_code=auction.source_auction_country_code,
        source_auction_country_name=auction.source_auction_country_name,
        auction_type_label=auction.auction_type_label,
        auction_status_label=auction.auction_status_label,
        observed_at=timestamp,
    )

    prices_inserted = 0
    events_inserted = 0
    lots_upserted = 0

    for car_id, car, lot in validated_updates:
        database.upsert_auction_lot(
            car_id,
            auction.exleasingcar_auction_id,
            displayed_lot_reference=lot.displayed_lot_reference,
            listing_code_attribute=lot.listing_code_attribute,
            observed_at=timestamp,
        )
        lots_upserted += 1

        if car.highest_offer is not None:
            inserted = database.record_price_observation(
                car_id,
                car.highest_offer,
                observed_at=timestamp,
                exleasingcar_auction_id=auction.exleasingcar_auction_id,
                displayed_lot_reference=lot.displayed_lot_reference,
                listing_code_attribute=lot.listing_code_attribute,
            )
            prices_inserted += int(inserted)

        # A False final_call_active value alone is not enough to create an
        # event: the API parser currently uses False when timer data is absent.
        # Save an event when another state field exists or FINAL CALL is true.
        has_meaningful_event = any(
            (
                car.auction_phase is not None,
                car.bid_status is not None,
                car.final_call_active,
                car.live_bidding is not None,
                car.auction_end_at is not None,
            )
        )

        if has_meaningful_event:
            inserted = database.record_auction_event(
                car_id,
                auction_phase=car.auction_phase,
                bid_status=car.bid_status,
                final_call_active=car.final_call_active,
                live_bidding=car.live_bidding,
                auction_end_at=car.auction_end_at,
                remaining_seconds=car.remaining_seconds,
                observed_at=timestamp,
                exleasingcar_auction_id=auction.exleasingcar_auction_id,
                displayed_lot_reference=lot.displayed_lot_reference,
                listing_code_attribute=lot.listing_code_attribute,
            )
            events_inserted += int(inserted)

    return IngestionSummary(
        cars_processed=len(updates),
        lots_upserted=lots_upserted,
        prices_inserted=prices_inserted,
        events_inserted=events_inserted,
    )
