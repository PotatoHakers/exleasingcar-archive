"""Collect auction snapshots through a manually authenticated Playwright browser.

The collector deliberately does not automate sign-in, solve CAPTCHAs, or reuse
cookies from the user's everyday browser profile. It opens its own persistent
Chromium profile, where the user signs in manually once. Subsequent runs reuse
that local profile until the website session expires.
"""

from __future__ import annotations

import argparse
import re
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from .api_parser import parse_car_update_response
from .database import ArchiveDatabase
from .ingestion import AuctionContext, LotMetadata, store_car_updates
from .metadata_parser import (
    ParsedAuctionListing,
    ParsedAuctionLotMetadata,
    parse_auction_catalog_metadata,
    parse_auction_listings,
    parse_catalog_pagination,
    parse_vehicle_metadata,
)

AUCTION_LIST_URL = "https://www.exleasingcar.com/ru/авто-аукционы"
API_ENDPOINT_MARKER = "ajax_update_car_form.v3.1.php"
DEFAULT_DATABASE = Path("data/fresh_api_test.sqlite3")
DEFAULT_PROFILE_DIR = Path("data/browser_profile")
DEFAULT_RAW_DIR = Path("data/raw/collector")


@dataclass(frozen=True, slots=True)
class CollectorSummary:
    auctions_discovered: int
    auctions_processed: int
    catalog_pages_processed: int
    lots_seen: int
    detail_pages_processed: int
    api_responses_seen: int
    api_cars_processed: int
    prices_inserted: int
    seller_price_references_inserted: int
    events_inserted: int


@dataclass(frozen=True, slots=True)
class CapturedApiResponse:
    url: str
    payload: Mapping[str, Any]


class ApiResponseRecorder:
    """Keep parsed endpoint JSON in memory while a browser page is loading."""

    def __init__(self) -> None:
        self.responses: list[CapturedApiResponse] = []
        self.errors: list[str] = []

    def attach(self, context: Any) -> None:
        context.on("response", self._on_response)

    def _on_response(self, response: Any) -> None:
        if API_ENDPOINT_MARKER not in response.url:
            return
        if not response.ok:
            self.errors.append(f"HTTP {response.status}: {response.url}")
            return
        try:
            response.finished()
            payload = response.json()
        except Exception as exc:  # Playwright response/body may disappear on navigation.
            self.errors.append(f"Could not read JSON from {response.url}: {exc}")
            return
        if not isinstance(payload, Mapping):
            self.errors.append(f"Unexpected JSON response shape: {response.url}")
            return
        self.responses.append(CapturedApiResponse(response.url, payload))


def filter_updates_for_catalog(
    updates: Mapping[str, Any],
    catalog_car_ids: set[str],
) -> dict[str, Any]:
    """Return only API entries whose IDs were found in this auction catalogue."""
    return {
        str(car_id): update
        for car_id, update in updates.items()
        if str(car_id) in catalog_car_ids
    }


def extract_vehicle_detail_urls(html: str, base_url: str) -> dict[str, str]:
    """Find vehicle detail URLs from links or the site's card click handler.

    Some Exleasingcar catalogue cards do not contain a normal detail anchor.
    Instead, the card has ``onclick`` JavaScript that sets an
    ``after_login_redirect`` URL and opens a lightbox. In that layout the only
    authoritative detail URL is embedded in the click handler.
    """
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, str] = {}
    onclick_detail_pattern = re.compile(
        r"(?P<url>(?:https?://|/)[^\"']*/auto-details/(?P<car_id>[^/?#\"']+))",
        re.IGNORECASE,
    )

    for card in soup.select("[car-id]"):
        car_id_value = card.get("car-id")
        if car_id_value is None:
            continue
        car_id = str(car_id_value).strip()
        if not car_id or car_id in result:
            continue

        detail_url: str | None = None
        link = card.find("a", href=re.compile(r"/auto-details/", re.IGNORECASE))
        if isinstance(link, Tag):
            href = str(link.get("href", "")).strip()
            if href:
                detail_url = urljoin(base_url, href)

        if detail_url is None:
            onclick = str(card.get("onclick", ""))
            match = onclick_detail_pattern.search(onclick)
            if match is not None:
                # The ID in the embedded URL must match the card ID. This
                # prevents accidentally associating another vehicle's URL.
                embedded_car_id = match.group("car_id").strip()
                if embedded_car_id == car_id:
                    detail_url = urljoin(base_url, match.group("url"))

        if detail_url is not None:
            result[car_id] = detail_url

    return result


def _auction_context(listing: ParsedAuctionListing) -> AuctionContext:
    return AuctionContext(
        exleasingcar_auction_id=listing.exleasingcar_auction_id,
        source_auction_name=listing.source_auction_name,
        source_auction_country_code=listing.source_auction_country_code,
        source_auction_country_name=listing.source_auction_country_name,
        auction_type_label=listing.auction_type_label,
        auction_status_label=listing.auction_status_label,
    )


def _lot_metadata(lot: ParsedAuctionLotMetadata) -> LotMetadata:
    return LotMetadata(
        displayed_lot_reference=lot.displayed_lot_reference,
        listing_code_attribute=lot.listing_code_attribute,
        mileage_km=lot.mileage_km,
    )


def _wait_for_dynamic_content(page: Any, timeout_ms: int = 7_000) -> None:
    """Give XHR responses time to complete without requiring network-idle forever."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        # Some pages keep analytics/notifications requests open. DOM content and
        # endpoint responses are still handled; this is only a bounded wait.
        page.wait_for_timeout(1_000)


def _safe_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "item"


class ExleasingcarCollector:
    """Read auction/catalogue pages and persist their public data in SQLite."""

    def __init__(
        self,
        database: ArchiveDatabase,
        raw_dir: Path,
        *,
        page_delay_seconds: float = 1.5,
    ) -> None:
        if page_delay_seconds < 0:
            raise ValueError("page_delay_seconds cannot be negative")
        self.database = database
        self.raw_dir = raw_dir
        self.page_delay_seconds = page_delay_seconds
        self.recorder = ApiResponseRecorder()
        self.api_cursor = 0
        self.api_responses_seen = 0
        self.api_cars_processed = 0
        self.prices_inserted = 0
        self.price_references_inserted = 0
        self.events_inserted = 0

    def _save_html(self, path: Path, html: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")

    def _persist_api_updates(
        self,
        auction: AuctionContext,
        catalog_car_ids: set[str],
        lot_metadata: Mapping[str, LotMetadata],
    ) -> None:
        """Ingest new API responses, keeping auction/vehicle relationships strict."""
        while self.api_cursor < len(self.recorder.responses):
            captured = self.recorder.responses[self.api_cursor]
            self.api_cursor += 1
            self.api_responses_seen += 1
            try:
                updates = parse_car_update_response(captured.payload)
            except (TypeError, ValueError) as exc:
                print(f"  Skipped malformed API response: {exc}")
                continue

            selected = filter_updates_for_catalog(updates, catalog_car_ids)
            if not selected:
                continue

            summary = store_car_updates(
                self.database,
                selected,
                auction,
                lot_metadata={
                    car_id: lot_metadata[car_id]
                    for car_id in selected
                    if car_id in lot_metadata
                },
            )
            self.api_cars_processed += summary.cars_processed
            self.prices_inserted += summary.prices_inserted
            self.price_references_inserted += summary.price_references_inserted
            self.events_inserted += summary.events_inserted

    def _upsert_catalog_lots(
        self,
        auction: AuctionContext,
        lots: tuple[ParsedAuctionLotMetadata, ...],
        observed_at: datetime,
    ) -> dict[str, LotMetadata]:
        result: dict[str, LotMetadata] = {}
        for lot in lots:
            self.database.upsert_vehicle(
                lot.source_car_id,
                make=lot.make_label,
                displayed_title=lot.displayed_title,
                observed_at=observed_at,
            )
            self.database.upsert_auction_lot(
                lot.source_car_id,
                auction.exleasingcar_auction_id,
                displayed_lot_reference=lot.displayed_lot_reference,
                listing_code_attribute=lot.listing_code_attribute,
                mileage_km=lot.mileage_km,
                observed_at=observed_at,
            )
            result[lot.source_car_id] = _lot_metadata(lot)
        return result

    def _enrich_vehicle_detail(
        self,
        auction: AuctionContext,
        car_id: str,
        detail_url: str,
        page: Any,
        observed_at: datetime,
        snapshot_dir: Path,
    ) -> bool:
        try:
            page.goto(detail_url, wait_until="domcontentloaded", timeout=45_000)
            _wait_for_dynamic_content(page)
        except Exception as exc:
            print(f"  Detail page {car_id} failed: {exc}")
            return False

        html = page.content()
        self._save_html(snapshot_dir / f"vehicle_{_safe_component(car_id)}.html", html)
        vehicle = parse_vehicle_metadata(html, page.url)
        if vehicle.source_car_id != car_id:
            print(
                f"  Skipped detail {car_id}: page ID is {vehicle.source_car_id!r}"
            )
            return False
        if (
            vehicle.exleasingcar_auction_id is not None
            and vehicle.exleasingcar_auction_id != auction.exleasingcar_auction_id
        ):
            print(
                f"  Skipped detail {car_id}: auction ID mismatch "
                f"{vehicle.exleasingcar_auction_id} != {auction.exleasingcar_auction_id}"
            )
            return False

        self.database.upsert_vehicle(
            car_id,
            displayed_title=vehicle.title,
            chassis_number=vehicle.chassis_number,
            chassis_number_fragment=vehicle.chassis_number_fragment,
            chassis_number_masked=vehicle.chassis_number_masked,
            location_text=vehicle.location_text,
            location_country_code=vehicle.location_country_code,
            location_country_name=vehicle.location_country_name,
            last_registration_country_code=vehicle.last_registration_country_code,
            last_registration_country_name=vehicle.last_registration_country_name,
            observed_at=observed_at,
        )
        return True

    def _collect_auction(
        self,
        listing: ParsedAuctionListing,
        page: Any,
        snapshot_dir: Path,
        *,
        max_detail_pages: int,
    ) -> tuple[int, int, int]:
        if not listing.source_url:
            print(f"Auction {listing.exleasingcar_auction_id}: no source URL; skipped")
            return 0, 0, 0

        auction = _auction_context(listing)
        self.database.upsert_auction(
            listing.exleasingcar_auction_id,
            source_auction_name=listing.source_auction_name,
            source_auction_country_code=listing.source_auction_country_code,
            source_auction_country_name=listing.source_auction_country_name,
            auction_type_label=listing.auction_type_label,
            auction_status_label=listing.auction_status_label,
        )
        auction_dir = snapshot_dir / f"auction_{_safe_component(listing.exleasingcar_auction_id)}"
        auction_dir.mkdir(parents=True, exist_ok=True)

        current_url: str | None = listing.source_url
        visited_urls: set[str] = set()
        all_lot_metadata: dict[str, LotMetadata] = {}
        detail_urls: dict[str, str] = {}
        pages_processed = 0
        lots_seen = 0
        details_processed = 0
        observed_at = datetime.now(timezone.utc)

        while current_url and current_url not in visited_urls:
            visited_urls.add(current_url)
            try:
                page.goto(current_url, wait_until="domcontentloaded", timeout=45_000)
                _wait_for_dynamic_content(page)
            except Exception as exc:
                print(f"  Catalogue page failed ({current_url}): {exc}")
                break

            html = page.content()
            pages_processed += 1
            self._save_html(
                auction_dir / f"catalog_page_{pages_processed:03d}.html",
                html,
            )
            catalog = parse_auction_catalog_metadata(html, page.url)
            if catalog.exleasingcar_auction_id != listing.exleasingcar_auction_id:
                raise ValueError(
                    "Catalogue/auction mismatch: "
                    f"expected {listing.exleasingcar_auction_id}, "
                    f"received {catalog.exleasingcar_auction_id!r}"
                )

            page_lot_metadata = self._upsert_catalog_lots(
                auction,
                catalog.lots,
                observed_at,
            )
            all_lot_metadata.update(page_lot_metadata)
            lots_seen += len(catalog.lots)
            detail_urls.update(extract_vehicle_detail_urls(html, page.url))

            # The page's AJAX responses are parsed only after its catalog IDs are
            # known, so unrelated IDs can never be attached to this auction.
            self._persist_api_updates(
                auction,
                set(all_lot_metadata),
                all_lot_metadata,
            )

            pagination = parse_catalog_pagination(html, page.url)
            next_url = pagination.next_page_url
            if next_url is None:
                next_candidates = [
                    candidate
                    for candidate in pagination.page_urls
                    if candidate not in visited_urls
                ]
                next_url = next_candidates[0] if next_candidates else None
            current_url = next_url

            if self.page_delay_seconds:
                time.sleep(self.page_delay_seconds)

        # Process any endpoint responses that finished as the final page closed.
        self._persist_api_updates(auction, set(all_lot_metadata), all_lot_metadata)

        if (
            listing.vehicle_count is not None
            and listing.vehicle_count > len(all_lot_metadata)
        ):
            print(
                f"  WARNING: auction list reports {listing.vehicle_count} vehicles, "
                f"but only {len(all_lot_metadata)} unique lot IDs were found. "
                "Catalog may be incomplete."
            )

        if max_detail_pages != 0:
            detail_items = list(detail_urls.items())
            if max_detail_pages > 0:
                detail_items = detail_items[:max_detail_pages]
            for car_id, detail_url in detail_items:
                if self._enrich_vehicle_detail(
                    auction,
                    car_id,
                    detail_url,
                    page,
                    observed_at,
                    auction_dir,
                ):
                    details_processed += 1
                self._persist_api_updates(
                    auction,
                    set(all_lot_metadata),
                    all_lot_metadata,
                )
                if self.page_delay_seconds:
                    time.sleep(self.page_delay_seconds)

        print(
            f"Auction {listing.exleasingcar_auction_id}: "
            f"{pages_processed} page(s), {lots_seen} lot card(s), "
            f"{details_processed} detail page(s)"
        )
        return pages_processed, lots_seen, details_processed

    def run(
        self,
        *,
        auction_list_url: str,
        profile_dir: Path,
        max_auctions: int,
        auction_id: str | None,
        max_detail_pages: int,
    ) -> CollectorSummary:
        """Open the persistent browser and collect a bounded auction scan."""
        if max_auctions < 0:
            raise ValueError("max_auctions must be zero (all) or a positive integer")
        if max_detail_pages < -1:
            raise ValueError("max_detail_pages must be -1 (all), 0 (none), or positive")

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SystemExit(
                "Playwright is not installed. Run: python -m pip install playwright"
            ) from exc

        self.database.initialize()
        run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        snapshot_dir = self.raw_dir / run_stamp
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        profile_was_initialized = profile_dir.exists() and any(profile_dir.iterdir())
        profile_dir.mkdir(parents=True, exist_ok=True)

        pages_processed = 0
        lots_seen = 0
        details_processed = 0

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=False,
                viewport={"width": 1440, "height": 1000},
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                self.recorder.attach(context)
                print("Opening Exleasingcar in the collector's own browser profile...")
                page.goto(auction_list_url, wait_until="domcontentloaded", timeout=60_000)
                _wait_for_dynamic_content(page)
                auctions_html = page.content()
                auctions = parse_auction_listings(auctions_html, page.url)

                if not profile_was_initialized:
                    print(
                        "This is a new collector browser profile. Sign in manually "
                        "in the opened browser window if Exleasingcar asks for it."
                    )
                    input("When the auction list is ready, press Enter to continue: ")
                    page.goto(auction_list_url, wait_until="domcontentloaded", timeout=60_000)
                    _wait_for_dynamic_content(page)
                    auctions_html = page.content()
                    auctions = parse_auction_listings(auctions_html, page.url)
                elif not auctions:
                    print(
                        "No auction cards are visible. Sign in manually in the opened "
                        "browser window, then return here."
                    )
                    input("After login, press Enter to retry the auction list: ")
                    page.goto(auction_list_url, wait_until="domcontentloaded", timeout=60_000)
                    _wait_for_dynamic_content(page)
                    auctions_html = page.content()
                    auctions = parse_auction_listings(auctions_html, page.url)

                if not auctions:
                    raise RuntimeError(
                        "No auction cards were found after login. Check the page "
                        "address and whether the account can access the auction list."
                    )

                self._save_html(snapshot_dir / "auction_list.html", auctions_html)
                # Do not attribute endpoint traffic generated by the auction-list
                # page to the first auction. Capture auction-specific requests only.
                self.api_cursor = len(self.recorder.responses)
                for listing in auctions:
                    self.database.upsert_auction(
                        listing.exleasingcar_auction_id,
                        source_auction_name=listing.source_auction_name,
                        source_auction_country_code=listing.source_auction_country_code,
                        source_auction_country_name=listing.source_auction_country_name,
                        auction_type_label=listing.auction_type_label,
                        auction_status_label=listing.auction_status_label,
                    )

                print(f"Auction cards discovered: {len(auctions)}")
                selected_auctions = auctions
                if auction_id:
                    selected_auctions = [
                        item
                        for item in auctions
                        if item.exleasingcar_auction_id == auction_id
                    ]
                    if not selected_auctions:
                        raise ValueError(
                            f"Auction {auction_id} is not present on the current list page"
                        )
                if max_auctions > 0:
                    selected_auctions = selected_auctions[:max_auctions]

                for listing in selected_auctions:
                    page_count, lot_count, detail_count = self._collect_auction(
                        listing,
                        page,
                        snapshot_dir,
                        max_detail_pages=max_detail_pages,
                    )
                    pages_processed += page_count
                    lots_seen += lot_count
                    details_processed += detail_count

                print(f"Raw snapshots saved under: {snapshot_dir}")
            finally:
                context.close()

        return CollectorSummary(
            auctions_discovered=len(auctions),
            auctions_processed=len(selected_auctions),
            catalog_pages_processed=pages_processed,
            lots_seen=lots_seen,
            detail_pages_processed=details_processed,
            api_responses_seen=self.api_responses_seen,
            api_cars_processed=self.api_cars_processed,
            prices_inserted=self.prices_inserted,
            seller_price_references_inserted=self.price_references_inserted,
            events_inserted=self.events_inserted,
        )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Scan Exleasingcar auctions via a manually authenticated browser and "
            "store public auction data in SQLite."
        )
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--auction-list-url", default=AUCTION_LIST_URL)
    parser.add_argument("--auction-id", help="Process only this auction ID")
    parser.add_argument(
        "--max-auctions",
        type=int,
        default=1,
        help="Maximum auctions to scan; 0 means all auctions on the list page (default: 1)",
    )
    parser.add_argument(
        "--max-detail-pages",
        type=int,
        default=5,
        help="Detail pages per auction: -1 all, 0 none, positive number (default: 5)",
    )
    parser.add_argument(
        "--page-delay",
        type=float,
        default=1.5,
        help="Seconds to pause between page navigations (default: 1.5)",
    )
    return parser


def main() -> None:
    args = build_argument_parser().parse_args()
    if args.max_auctions < 0:
        raise SystemExit("--max-auctions must be zero (all) or a positive integer")
    if args.max_detail_pages < -1:
        raise SystemExit("--max-detail-pages must be -1, 0 or a positive integer")
    if args.page_delay < 0:
        raise SystemExit("--page-delay cannot be negative")

    collector = ExleasingcarCollector(
        ArchiveDatabase(args.database),
        args.raw_dir,
        page_delay_seconds=args.page_delay,
    )
    summary = collector.run(
        auction_list_url=args.auction_list_url,
        profile_dir=args.profile_dir,
        max_auctions=args.max_auctions,
        auction_id=args.auction_id,
        max_detail_pages=args.max_detail_pages,
    )
    print("\nCollection completed")
    for field, value in asdict(summary).items():
        print(f"  {field}: {value}")
    if collector.recorder.errors:
        print("API response capture warnings:")
        for message in collector.recorder.errors[:20]:
            print(f"  {message}")


if __name__ == "__main__":
    main()
