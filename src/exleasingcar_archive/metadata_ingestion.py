"""Import saved Exleasingcar HTML metadata into the SQLite archive."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from .api_parser import parse_car_update_response
from .database import ArchiveDatabase
from .detail_parser import parse_vehicle_detail
from .ingestion import AuctionContext, IngestionSummary, LotMetadata, store_car_updates
from .metadata_parser import (
    ParsedAuctionListing,
    ParsedAuctionLotMetadata,
    parse_auction_catalog_metadata,
    parse_auction_listings,
    parse_catalog_pagination,
    parse_vehicle_metadata,
    parse_vehicle_media,
)


@dataclass(frozen=True, slots=True)
class MetadataImportSummary:
    """Counts of metadata records inserted or updated from saved HTML."""

    auctions_seen: int
    lots_seen: int
    vehicle_details_enriched: int
    selected_auction_id: str
    selected_car_id: str
    catalog_pages_imported: int
    missing_catalog_page_urls: tuple[str, ...]
    api_ingestion: IngestionSummary | None = None
    sale_type_recognized: bool = False
    lot_price_observations_inserted: int = 0
    media_sources_registered: int = 0


def _auction_context(listing: ParsedAuctionListing) -> AuctionContext:
    return AuctionContext(
        exleasingcar_auction_id=listing.exleasingcar_auction_id,
        source_auction_name=listing.source_auction_name,
        source_auction_country_code=listing.source_auction_country_code,
        source_auction_country_name=listing.source_auction_country_name,
        auction_type_label=listing.auction_type_label,
        auction_status_label=listing.auction_status_label,
        markers=listing.markers,
    )


def _lot_context(lot: ParsedAuctionLotMetadata) -> LotMetadata:
    return LotMetadata(
        displayed_lot_reference=lot.displayed_lot_reference,
        listing_code_attribute=lot.listing_code_attribute,
        mileage_km=lot.mileage_km,
    )


def import_html_snapshots(
    database: ArchiveDatabase,
    *,
    auction_list_html: str,
    catalog_html: str,
    vehicle_detail_html: str,
    observed_at: datetime | None = None,
    auction_list_url: str | None = None,
    catalog_url: str | None = None,
    additional_catalog_htmls: Sequence[str] = (),
    additional_catalog_urls: Sequence[str | None] = (),
    vehicle_detail_url: str | None = None,
    api_response: str | Mapping[str, object] | None = None,
) -> MetadataImportSummary:
    """Parse and store one saved auction-list, catalogue, and vehicle page.

    All page relationships are validated before the first database write. Raw
    HTML is parsed in memory and is not copied into the database.

    If ``api_response`` is supplied, every car ID in that response must be
    present in the parsed catalogue. This prevents assigning prices to a
    guessed or unrelated auction lot.
    """
    if not isinstance(database, ArchiveDatabase):
        raise TypeError("database must be an ArchiveDatabase")

    timestamp = observed_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")

    auctions = parse_auction_listings(auction_list_html, auction_list_url)
    catalog_html_pages = [catalog_html, *additional_catalog_htmls]
    if additional_catalog_urls and len(additional_catalog_urls) != len(additional_catalog_htmls):
        raise ValueError("additional_catalog_urls must match additional_catalog_htmls")
    catalog_url_pages = [catalog_url, *additional_catalog_urls]
    if not additional_catalog_urls:
        catalog_url_pages.extend([None] * len(additional_catalog_htmls))

    catalogs = [
        parse_auction_catalog_metadata(page_html, page_url)
        for page_html, page_url in zip(catalog_html_pages, catalog_url_pages)
    ]
    catalog = catalogs[0]
    vehicle = parse_vehicle_metadata(vehicle_detail_html, vehicle_detail_url)

    if catalog.exleasingcar_auction_id is None:
        raise ValueError("Could not determine Exleasingcar auction ID from catalogue")
    if vehicle.source_car_id is None:
        raise ValueError("Could not determine vehicle ID from detail page")
    if vehicle.exleasingcar_auction_id is not None and (
        vehicle.exleasingcar_auction_id != catalog.exleasingcar_auction_id
    ):
        raise ValueError(
            "Vehicle detail auction ID does not match catalogue auction ID: "
            f"{vehicle.exleasingcar_auction_id} != {catalog.exleasingcar_auction_id}"
        )

    for page_number, page_catalog in enumerate(catalogs[1:], start=2):
        if page_catalog.exleasingcar_auction_id != catalog.exleasingcar_auction_id:
            raise ValueError(
                f"Catalogue page {page_number} belongs to auction "
                f"{page_catalog.exleasingcar_auction_id!r}, expected "
                f"{catalog.exleasingcar_auction_id!r}"
            )

    lots_by_car_id: dict[str, ParsedAuctionLotMetadata] = {}
    for page_catalog in catalogs:
        for lot in page_catalog.lots:
            existing = lots_by_car_id.get(lot.source_car_id)
            if existing is None:
                lots_by_car_id[lot.source_car_id] = lot
                continue
            lots_by_car_id[lot.source_car_id] = ParsedAuctionLotMetadata(
                source_car_id=lot.source_car_id,
                displayed_title=lot.displayed_title or existing.displayed_title,
                displayed_lot_reference=(
                    lot.displayed_lot_reference or existing.displayed_lot_reference
                ),
                listing_code_attribute=(
                    lot.listing_code_attribute or existing.listing_code_attribute
                ),
                make_label=lot.make_label or existing.make_label,
                mileage_km=(
                    lot.mileage_km
                    if lot.mileage_km is not None
                    else existing.mileage_km
                ),
            )

    # The page's active pagination link identifies which page was supplied.
    # Compare those URLs with all links discovered on the provided pages.
    pagination_info = [
        parse_catalog_pagination(page_html, page_url)
        for page_html, page_url in zip(catalog_html_pages, catalog_url_pages)
    ]
    discovered_page_urls = {
        url for page in pagination_info for url in page.page_urls
    }
    supplied_page_urls = {
        page.current_page_url
        for page in pagination_info
        if page.current_page_url is not None
    }
    missing_catalog_page_urls = tuple(sorted(discovered_page_urls - supplied_page_urls))

    if vehicle.source_car_id not in lots_by_car_id:
        raise ValueError(
            "Vehicle detail ID is not present in the supplied auction catalogue: "
            f"{vehicle.source_car_id}"
        )

    listing_by_id = {item.exleasingcar_auction_id: item for item in auctions}
    selected_listing = listing_by_id.get(catalog.exleasingcar_auction_id)
    if selected_listing is None:
        # The auction-list HTML can be paginated or captured at a different
        # moment. Keep the known ID, but do not invent the missing metadata.
        selected_context = AuctionContext(
            exleasingcar_auction_id=catalog.exleasingcar_auction_id
        )
    else:
        selected_context = _auction_context(selected_listing)

    parsed_api_updates = None
    if api_response is not None:
        parsed_api_updates = parse_car_update_response(api_response)
        unknown_api_ids = sorted(set(parsed_api_updates) - set(lots_by_car_id))
        if unknown_api_ids:
            raise ValueError(
                "API response contains car IDs outside the supplied catalogue: "
                + ", ".join(unknown_api_ids[:10])
            )

    # Store all auction cards from the list page. The unique external ID makes
    # this safe to repeat when a snapshot is imported more than once.
    for listing in auctions:
        database.upsert_auction(
            listing.exleasingcar_auction_id,
            source_auction_name=listing.source_auction_name,
            source_auction_country_code=listing.source_auction_country_code,
            source_auction_country_name=listing.source_auction_country_name,
            auction_type_label=listing.auction_type_label,
            auction_status_label=listing.auction_status_label,
            markers=listing.markers,
            observed_at=timestamp,
        )

    # Keep the selected auction even when it was absent from the list snapshot.
    database.upsert_auction(
        selected_context.exleasingcar_auction_id,
        source_auction_name=selected_context.source_auction_name,
        source_auction_country_code=selected_context.source_auction_country_code,
        source_auction_country_name=selected_context.source_auction_country_name,
        auction_type_label=selected_context.auction_type_label,
        auction_status_label=selected_context.auction_status_label,
        markers=selected_context.markers,
        observed_at=timestamp,
    )

    for lot in lots_by_car_id.values():
        database.upsert_vehicle(
            lot.source_car_id,
            make=lot.make_label,
            displayed_title=lot.displayed_title,
            observed_at=timestamp,
        )
        database.upsert_auction_lot(
            lot.source_car_id,
            catalog.exleasingcar_auction_id,
            displayed_lot_reference=lot.displayed_lot_reference,
            listing_code_attribute=lot.listing_code_attribute,
            mileage_km=lot.mileage_km,
            observed_at=timestamp,
        )

    database.upsert_vehicle(
        vehicle.source_car_id,
        displayed_title=vehicle.title,
        chassis_number=vehicle.chassis_number,
        chassis_number_fragment=vehicle.chassis_number_fragment,
        chassis_number_masked=vehicle.chassis_number_masked,
        location_text=vehicle.location_text,
        location_country_code=vehicle.location_country_code,
        location_country_name=vehicle.location_country_name,
        last_registration_country_code=vehicle.last_registration_country_code,
        last_registration_country_name=vehicle.last_registration_country_name,
        observed_at=timestamp,
    )

    media_references = parse_vehicle_media(
        vehicle_detail_html,
        vehicle_detail_url,
    )
    for reference in media_references:
        database.upsert_lot_media_source(
            vehicle.source_car_id,
            catalog.exleasingcar_auction_id,
            reference.media_type,
            reference.source_url,
            preview_url=reference.preview_url,
            label=reference.label,
            source_order=reference.source_order,
            discovered_at=timestamp,
        )

    parsed_detail = parse_vehicle_detail(
        vehicle_detail_html,
        source_car_id=vehicle.source_car_id,
        page_url=vehicle_detail_url,
    )
    if parsed_detail.sale_type is not None:
        database.upsert_auction_lot(
            vehicle.source_car_id,
            catalog.exleasingcar_auction_id,
            sale_type=parsed_detail.sale_type,
            observed_at=timestamp,
        )

    lot_prices_inserted = 0
    for price in (
        parsed_detail.displayed_auction_price,
        parsed_detail.fixed_price,
    ):
        if price is None:
            continue
        lot_prices_inserted += int(
            database.record_lot_price_observation(
                vehicle.source_car_id,
                price,
                observed_at=timestamp,
                exleasingcar_auction_id=catalog.exleasingcar_auction_id,
                sale_type=parsed_detail.sale_type,
            )
        )

    api_summary = None
    if parsed_api_updates is not None:
        api_summary = store_car_updates(
            database,
            parsed_api_updates,
            selected_context,
            observed_at=timestamp,
            lot_metadata={
                car_id: _lot_context(lot)
                for car_id, lot in lots_by_car_id.items()
            },
        )

    return MetadataImportSummary(
        auctions_seen=len(auctions),
        lots_seen=len(lots_by_car_id),
        vehicle_details_enriched=1,
        selected_auction_id=catalog.exleasingcar_auction_id,
        selected_car_id=vehicle.source_car_id,
        catalog_pages_imported=len(catalog_html_pages),
        missing_catalog_page_urls=missing_catalog_page_urls,
        api_ingestion=api_summary,
        sale_type_recognized=parsed_detail.sale_type is not None,
        lot_price_observations_inserted=lot_prices_inserted,
        media_sources_registered=len(media_references),
    )
