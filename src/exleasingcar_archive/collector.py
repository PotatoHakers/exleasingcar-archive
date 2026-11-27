"""Collect auction snapshots through a manually authenticated Playwright browser.

The collector uses its own persistent Chromium profile and retrieves the
account credentials from the operating-system credential store via keyring.
It performs the ordinary website login form and the site's visible
"Continue" acknowledgement when the session expires. It does not solve
CAPTCHAs, bypass additional verification, or extract passwords from Chrome.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from .api_parser import parse_car_update_response
from .database import ArchiveDatabase, BootstrapQueueItem
from .detail_parser import parse_vehicle_detail
from .ingestion import AuctionContext, LotMetadata, store_car_updates
from .metadata_parser import (
    ParsedAuctionListing,
    ParsedAuctionLotMetadata,
    ParsedCatalogPagination,
    parse_auction_catalog_metadata,
    parse_auction_listings,
    parse_catalog_pagination,
    parse_vehicle_media,
    parse_vehicle_metadata,
)
from .models import PriceType, SaleType, VehicleStatus
from .parsers import ParsedLot, PriceObservation, parse_auction_list
from .process_lock import CollectorAlreadyRunning, CollectorProcessLock
from .scheduler import RunBudget, RunLimits

try:
    from playwright.sync_api import Error as PlaywrightError
except ImportError:  # Keep HTML parser tests usable without the optional browser extra.
    class PlaywrightError(RuntimeError):
        """Fallback exception type used only when Playwright is unavailable."""

AUCTION_LIST_URL = "https://www.exleasingcar.com/ru/авто-аукционы"
API_ENDPOINT_MARKER = "ajax_update_car_form.v3.1.php"
DEFAULT_DATABASE = Path("data/fresh_api_test.sqlite3")
DEFAULT_PROFILE_DIR = Path("data/browser_profile")
DEFAULT_RAW_DIR = Path("data/raw/collector")
KEYRING_SERVICE = "exleasingcar-archive"
KEYRING_USERNAME_KEY = "login"
KEYRING_PASSWORD_KEY = "password"


class CataloguePageMismatchError(ValueError):
    """A requested auction catalogue returned a different page."""

    def __init__(
        self,
        expected_auction_id: str,
        received_auction_id: str | None,
        page_kind: str,
    ) -> None:
        self.expected_auction_id = expected_auction_id
        self.received_auction_id = received_auction_id
        self.page_kind = page_kind
        super().__init__(
            "Catalogue/auction mismatch: "
            f"expected {expected_auction_id}, received {received_auction_id!r} "
            f"(page_kind={page_kind})"
        )


def _safe_diagnostic_url(value: str | None) -> str | None:
    """Keep URL paths for diagnostics without persisting query strings or fragments."""
    if not value:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _next_unvisited_catalog_page_url(
    pagination: ParsedCatalogPagination,
    *,
    current_page_url: str,
    visited_page_urls: set[str],
) -> str | None:
    """Choose a forward pagination URL without revisiting saved pages.

    Some catalogue pages advertise a stale pagination-next link that
    points to a page already checkpointed in SQLite. Prefer that link only
    while it is new, then inspect the remaining numbered links in page order.
    """
    candidates = (pagination.next_page_url, *pagination.page_urls)
    considered: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in considered:
            continue
        considered.add(candidate)
        if candidate != current_page_url and candidate not in visited_page_urls:
            return candidate
    return None


@dataclass(frozen=True, slots=True)
class CollectorSummary:
    auctions_discovered: int
    auctions_processed: int
    catalog_pages_processed: int
    lots_seen: int
    detail_pages_processed: int
    api_responses_seen: int
    api_cars_processed: int
    prices_checked: int
    prices_inserted: int
    prices_unchanged: int
    seller_price_references_checked: int
    seller_price_references_inserted: int
    seller_price_references_unchanged: int
    events_checked: int
    events_inserted: int
    events_unchanged: int
    sale_types_recognized: int
    lot_price_observations_checked: int
    lot_price_observations_inserted: int
    lot_price_observations_unchanged: int
    qualified_lots: int
    lots_with_fixed_price: int
    excluded_unpriced_lots: int
    excluded_closed_lots: int
    excluded_unknown_lots: int
    auction_list_pages_processed: int = 0
    bootstrap_queue_total: int = 0
    bootstrap_queue_pending: int = 0
    bootstrap_queue_in_progress: int = 0
    bootstrap_queue_completed: int = 0
    bootstrap_queue_deferred: int = 0
    bootstrap_queue_blocked: int = 0


@dataclass(frozen=True, slots=True)
class CatalogDetailCoverage:
    unique_lot_ids: int
    repeated_card_occurrences: int
    detail_urls_found: int
    lot_ids_without_detail_url: tuple[str, ...]


def _catalog_detail_coverage(
    lots_seen: int,
    catalog_lot_ids: set[str],
    detail_url_ids: set[str],
) -> CatalogDetailCoverage:
    """Separate repeated catalogue cards from lots with no recognized detail URL."""
    return CatalogDetailCoverage(
        unique_lot_ids=len(catalog_lot_ids),
        repeated_card_occurrences=max(0, lots_seen - len(catalog_lot_ids)),
        detail_urls_found=len(detail_url_ids),
        lot_ids_without_detail_url=tuple(sorted(catalog_lot_ids - detail_url_ids)),
    )


def _catalog_observation_tie_key(lot: ParsedLot) -> tuple[Any, ...]:
    """Provide deterministic ordering when two catalogue snapshots share a timestamp."""
    price = lot.current_price
    return (
        int(lot.status is VehicleStatus.COMPLETED),
        lot.status.value,
        lot.raw_status or "",
        lot.is_hot_bid,
        lot.title or "",
        lot.source_url or "",
        price.price_type.value if price is not None else "",
        str(price.amount) if price is not None else "",
        (price.currency or "") if price is not None else "",
        price.raw_text if price is not None else "",
        price.source_field if price is not None else "",
    )


def _merge_catalog_lot_observation(
    observations: dict[str, tuple[ParsedLot, datetime]],
    lot: ParsedLot,
    observed_at: datetime,
) -> None:
    """Keep the newest observation, with an order-independent timestamp tie-break."""
    current = observations.get(lot.source_car_id)
    if current is None:
        observations[lot.source_car_id] = (lot, observed_at)
        return

    current_lot, current_observed_at = current
    if observed_at > current_observed_at or (
        observed_at == current_observed_at
        and _catalog_observation_tie_key(lot)
        > _catalog_observation_tie_key(current_lot)
    ):
        observations[lot.source_car_id] = (lot, observed_at)


@dataclass(frozen=True, slots=True)
class CapturedApiResponse:
    url: str
    payload: Mapping[str, Any]
    observed_at: datetime


class ApiResponseRecorder:
    """Capture target API bodies as soon as response headers arrive."""

    def __init__(self) -> None:
        self.responses: list[CapturedApiResponse] = []
        self.errors: list[str] = []
        self.pending_requests: dict[int, Any] = {}
        self._responses_captured: set[int] = set()

    def attach(self, context: Any) -> None:
        # Read the body in the response handler. Response.body() waits until the
        # body is available, while doing so before a later page navigation can
        # make Chromium discard the response resource.
        context.on("request", self._on_request)
        context.on("response", self._on_response_received)
        context.on("requestfinished", self._on_request_finished)
        context.on("requestfailed", self._on_request_failed)

    def _on_request(self, request: Any) -> None:
        if API_ENDPOINT_MARKER in request.url:
            self.pending_requests[id(request)] = request

    def _capture_response(
        self,
        response: Any,
        *,
        record_errors: bool = True,
    ) -> bool:
        if API_ENDPOINT_MARKER not in response.url:
            return False
        if not response.ok:
            if record_errors:
                self.errors.append(f"HTTP {response.status}: {response.url}")
            return False
        try:
            payload = json.loads(response.body())
        except (PlaywrightError, ValueError, TypeError, UnicodeDecodeError) as exc:
            if record_errors:
                content_type = getattr(response, "headers", {}).get(
                    "content-type", "unknown"
                )
                self.errors.append(
                    f"Could not read JSON from {response.url} "
                    f"(HTTP {response.status}, content-type={content_type}): {exc}"
                )
            return False
        if not isinstance(payload, Mapping):
            if record_errors:
                self.errors.append(f"Unexpected JSON response shape: {response.url}")
            return False
        self.responses.append(
            CapturedApiResponse(
                response.url,
                payload,
                observed_at=datetime.now(timezone.utc),
            )
        )
        return True

    def _on_response_received(self, response: Any) -> None:
        if API_ENDPOINT_MARKER not in response.url:
            return
        request = response.request
        request_id = id(request)
        # Try immediately while the page is still on this document, but do not
        # emit an error yet: the body may not be readable until requestfinished.
        if self._capture_response(response, record_errors=False):
            self._responses_captured.add(request_id)

    def _on_request_finished(self, request: Any) -> None:
        request_id = id(request)
        if self.pending_requests.pop(request_id, None) is None:
            return
        if request_id not in self._responses_captured:
            # Retry after the response body has fully downloaded. This covers
            # transient Network.getResponseBody failures in the response event.
            try:
                response = request.response()
            except PlaywrightError as exc:
                self.errors.append(
                    f"Could not access completed API response from {request.url}: {exc}"
                )
                self._responses_captured.discard(request_id)
                return
            if response is None:
                self.errors.append(
                    f"API request finished without an HTTP response: {request.url}"
                )
                self._responses_captured.discard(request_id)
                return
            self._capture_response(response)
        self._responses_captured.discard(request_id)

    def _on_request_failed(self, request: Any) -> None:
        request_id = id(request)
        if self.pending_requests.pop(request_id, None) is None:
            return
        if request_id in self._responses_captured:
            # The full response body was already retained successfully.
            self._responses_captured.discard(request_id)
            return
        failure = getattr(request, "failure", None)
        if callable(failure):
            failure = failure()
        self.errors.append(
            "API request failed before its response body could be captured: "
            f"{request.url} ({failure or 'unknown network failure'})"
        )
        self._responses_captured.discard(request_id)

    def wait_for_pending(self, page: Any, *, timeout_ms: int = 3_000) -> bool:
        """Wait briefly for target API bodies before the next page navigation."""
        if timeout_ms < 0:
            raise ValueError("timeout_ms cannot be negative")
        deadline = time.monotonic() + timeout_ms / 1000
        while self.pending_requests and time.monotonic() < deadline:
            page.wait_for_timeout(50)

        # A timeout is a pause signal, not a confirmed capture failure. The
        # caller must stop before navigation, while the request may still
        # complete and be captured by a later event.
        return not self.pending_requests

    # Compatibility hook for direct recorder tests.
    def _on_response(self, response: Any) -> None:
        self._capture_response(response)

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
        markers=listing.markers,
    )


def _lot_metadata(lot: ParsedAuctionLotMetadata) -> LotMetadata:
    return LotMetadata(
        displayed_lot_reference=lot.displayed_lot_reference,
        listing_code_attribute=lot.listing_code_attribute,
        mileage_km=lot.mileage_km,
    )


def _serialize_catalog_page_data(
    catalog_lots: tuple[ParsedAuctionLotMetadata, ...],
    status_lots: tuple[ParsedLot, ...],
    detail_urls: Mapping[str, str],
    observed_at: datetime,
) -> dict[str, Any]:
    """Keep only parsed public fields needed to rebuild a scan after restart."""
    serialized_status_lots = []
    for lot in status_lots:
        price = lot.current_price
        serialized_status_lots.append(
            {
                "source_car_id": lot.source_car_id,
                "title": lot.title,
                "source_url": lot.source_url,
                "status": lot.status.value,
                "raw_status": lot.raw_status,
                "is_hot_bid": lot.is_hot_bid,
                "current_price": (
                    {
                        "price_type": price.price_type.value,
                        "amount": str(price.amount),
                        "currency": price.currency,
                        "raw_text": price.raw_text,
                        "source_field": price.source_field,
                    }
                    if price is not None
                    else None
                ),
            }
        )
    return {
        "observed_at": observed_at.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        "lots_seen": len(catalog_lots),
        "catalog_lot_metadata": [asdict(lot) for lot in catalog_lots],
        "catalog_lots": serialized_status_lots,
        "detail_urls": dict(detail_urls),
    }


def _restore_catalog_page_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Persisted catalogue page timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _restore_catalog_page_data(
    page_data: Mapping[str, Any],
    *,
    fallback_observed_at: str | None = None,
) -> tuple[
    tuple[ParsedAuctionLotMetadata, ...],
    tuple[ParsedLot, ...],
    dict[str, str],
    datetime,
    int,
]:
    """Rebuild typed page observations from the JSON checkpoint."""
    observed_value = page_data.get("observed_at") or fallback_observed_at
    if observed_value is None:
        raise ValueError("Persisted catalogue page has no observation timestamp")
    observed_at = _restore_catalog_page_datetime(str(observed_value))
    metadata = tuple(
        ParsedAuctionLotMetadata(**item)
        for item in page_data.get("catalog_lot_metadata", [])
    )
    restored_lots = []
    for item in page_data.get("catalog_lots", []):
        raw_price = item.get("current_price")
        price = None
        if raw_price is not None:
            price = PriceObservation(
                price_type=PriceType(raw_price["price_type"]),
                amount=Decimal(str(raw_price["amount"])),
                currency=raw_price.get("currency"),
                raw_text=str(raw_price["raw_text"]),
                source_field=str(raw_price["source_field"]),
            )
        restored_lots.append(
            ParsedLot(
                source_car_id=str(item["source_car_id"]),
                title=item.get("title"),
                source_url=item.get("source_url"),
                status=VehicleStatus(item["status"]),
                raw_status=item.get("raw_status"),
                is_hot_bid=bool(item["is_hot_bid"]),
                current_price=price,
            )
        )
    detail_urls = {
        str(car_id): str(url)
        for car_id, url in page_data.get("detail_urls", {}).items()
    }
    lots_seen = int(page_data.get("lots_seen", len(metadata)))
    if lots_seen < 0:
        raise ValueError("Persisted catalogue lots_seen cannot be negative")
    return metadata, tuple(restored_lots), detail_urls, observed_at, lots_seen


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


_PRIVATE_SNAPSHOT_FIELD = re.compile(
    r"offer|bid|email|password|csrf|session|token|account|user_id|credit|"
    r"note|comment|question|message",
    re.IGNORECASE,
)


def sanitize_html_snapshot(html: str) -> str:
    """Remove known account-identifying and private form values from saved HTML.

    The live page is still parsed in memory before this function is used. This
    sanitized copy is only an optional diagnostic/archive artifact.
    """
    if not isinstance(html, str):
        raise TypeError("html must be a string")

    soup = BeautifulSoup(html, "html.parser")

    # The account dropdown contains the user's display name and account ID.
    for element in soup.select(
        ".module-user, #onesignal-slidedown-container, .onesignal-slidedown"
    ):
        element.decompose()

    for element in soup.find_all(["input", "textarea"]):
        identity = " ".join(
            [
                str(element.get("name", "")),
                str(element.get("id", "")),
                " ".join(str(item) for item in element.get("class", [])),
            ]
        )
        if _PRIVATE_SNAPSHOT_FIELD.search(identity):
            element.attrs.pop("value", None)
            if element.name == "textarea":
                element.clear()

        for attribute in tuple(element.attrs):
            if re.fullmatch(
                r"data-(?:user|account|email|token|session|auth).*",
                str(attribute),
                re.IGNORECASE,
            ):
                element.attrs.pop(attribute, None)

    sanitized = str(soup)
    # These globals and the OneSignal ID tag are embedded in inline scripts,
    # outside the account dropdown itself.
    sanitized = re.sub(
        r"""(?i)(__USER_ID__\s*=\s*)['"][^'"]*['"]""",
        r'\1""',
        sanitized,
    )
    sanitized = re.sub(
        r"""(?i)(__EMAIL__\s*=\s*)['"][^'"]*['"]""",
        r'\1""',
        sanitized,
    )
    sanitized = re.sub(
        r"""(?i)(OneSignal\.sendTag\(\s*['"]ID['"]\s*,\s*)['"][^'"]*['"]""",
        r'\1""',
        sanitized,
    )
    sanitized = re.sub(
        r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
        "[redacted-email]",
        sanitized,
        flags=re.IGNORECASE,
    )
    return sanitized


class ExleasingcarCollector:
    """Read auction/catalogue pages and persist their public data in SQLite."""

    def __init__(
        self,
        database: ArchiveDatabase,
        raw_dir: Path,
        *,
        page_delay_seconds: float = 1.5,
        save_html_snapshots: bool = False,
    ) -> None:
        if page_delay_seconds < 0:
            raise ValueError("page_delay_seconds cannot be negative")
        if not isinstance(save_html_snapshots, bool):
            raise TypeError("save_html_snapshots must be bool")
        self.database = database
        self.raw_dir = raw_dir
        self.page_delay_seconds = page_delay_seconds
        self.save_html_snapshots = save_html_snapshots
        self.recorder = ApiResponseRecorder()
        self.api_cursor = 0
        self.api_responses_seen = 0
        self.api_cars_processed = 0
        self.prices_checked = 0
        self.prices_inserted = 0
        self.prices_unchanged = 0
        self.price_references_checked = 0
        self.price_references_inserted = 0
        self.price_references_unchanged = 0
        self.events_checked = 0
        self.events_inserted = 0
        self.events_unchanged = 0
        self.sale_types_recognized = 0
        self.qualified_lots = 0
        self.lots_with_fixed_price = 0
        self.excluded_unpriced_lots = 0
        self.excluded_closed_lots = 0
        self.excluded_unknown_lots = 0
        self.lot_price_observations_checked = 0
        self.lot_price_observations_inserted = 0
        self.lot_price_observations_unchanged = 0
        # Kept for compatibility with the previous manual-auth test helper.
        # The normal collector flow now uses _authenticate instead.
        self._manual_reauth_prompted = False

    def _save_html(self, path: Path, html: str) -> None:
        if not self.save_html_snapshots:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sanitize_html_snapshot(html), encoding="utf-8")

    def _persist_api_updates(
        self,
        auction: AuctionContext,
        eligible_car_ids: set[str],
        lot_metadata: Mapping[str, LotMetadata],
        *,
        open_auction_car_ids: set[str] | None = None,
        open_auction_candidate_ids: set[str] | None = None,
        run_budget: RunBudget | None = None,
    ) -> None:
        """Persist queued public API snapshots without losing deferred responses.

        API bodies are checkpointed with their catalogue page. Each response is
        consumed only after its observations are stored. Unknown sale modes and
        exhausted live-lot budgets leave the response queued for a later run.
        """
        completed_before_run = self.database.get_completed_open_auction_lots(
            auction.exleasingcar_auction_id,
            eligible_car_ids,
        )
        active_open_ids = (
            set(open_auction_car_ids)
            if open_auction_car_ids is not None
            else {
                car_id
                for car_id, sale_type in self.database.get_collection_eligible_lots(
                    auction.exleasingcar_auction_id,
                    eligible_car_ids,
                ).items()
                if sale_type == SaleType.OPEN_AUCTION.value
            }
        ) - completed_before_run
        candidate_open_ids = set(open_auction_candidate_ids or ())
        completed_during_run: set[str] = set()

        def process_response(
            captured: CapturedApiResponse,
            *,
            page_id: int | None,
            response_index: int | None,
            deferred_update_id: int | None = None,
        ) -> bool:
            """Process one response; False means it must remain in the durable queue."""
            if run_budget is not None and run_budget.snapshot().expired:
                print("  API response deferred by the per-run wall-clock limit.")
                return False
            self.api_responses_seen += 1
            try:
                updates = parse_car_update_response(captured.payload)
            except (TypeError, ValueError) as exc:
                print(f"  Skipped malformed API response (kept in checkpoint): {exc}")
                if deferred_update_id is not None:
                    self.database.mark_catalog_api_lot_update_processed(
                        deferred_update_id,
                        observed_at=datetime.now(timezone.utc),
                    )
                elif page_id is not None and response_index is not None:
                    self.database.advance_catalog_api_response_cursor(
                        page_id, response_index
                    )
                else:
                    self.api_cursor += 1
                return True

            catalog_response_ids = set(updates) & set(lot_metadata)
            sale_types = self.database.get_auction_lot_sale_types(
                auction.exleasingcar_auction_id,
                catalog_response_ids,
            )
            unclassified_ids = {
                car_id
                for car_id in catalog_response_ids
                if car_id in sale_types and sale_types[car_id] is None
            }
            if unclassified_ids:
                # Split unresolved lots into their own durable queue entries.
                # This lets known lots in the same bulk response be processed
                # now, then replays the isolated lot snapshot after a detail
                # page establishes the sale mode.
                if page_id is not None:
                    self.database.defer_catalog_api_lot_updates(
                        page_id,
                        {
                            "url": captured.url,
                            "payload": captured.payload,
                            "observed_at": captured.observed_at,
                        },
                        unclassified_ids,
                    )
                print(
                    "  Deferred API updates until sale modes are known for: "
                    + ", ".join(sorted(unclassified_ids))
                )

            promotable_ids = {
                car_id
                for car_id, update in updates.items()
                if car_id in candidate_open_ids
                and car_id not in eligible_car_ids
                and car_id not in completed_before_run
                and car_id not in completed_during_run
                and update.has_positive_highest_offer
            }
            eligible_updates = {
                car_id
                for car_id in updates
                if car_id in eligible_car_ids
                and car_id not in completed_before_run
                and car_id not in completed_during_run
            }
            live_lot_ids = (
                (eligible_updates & active_open_ids) | promotable_ids
            )
            if run_budget is not None and live_lot_ids:
                blocked = run_budget.try_consume(live_lots=len(live_lot_ids))
                if blocked is not None:
                    print(
                        "  API response deferred by the per-run live-lot budget "
                        f"({blocked.value})."
                    )
                    return False

            for car_id in sorted(promotable_ids):
                promoted = self.database.set_auction_lot_qualification(
                    car_id,
                    auction.exleasingcar_auction_id,
                    sale_type=SaleType.OPEN_AUCTION,
                    has_fixed_price=False,
                    collection_eligible=True,
                    checked_at=captured.observed_at,
                )
                if promoted:
                    eligible_car_ids.add(car_id)
                    active_open_ids.add(car_id)
                    if open_auction_car_ids is not None:
                        open_auction_car_ids.add(car_id)
                    self.qualified_lots += 1
                    print(
                        f"  Open lot {car_id} qualified after a positive public bid."
                    )

            selected = filter_updates_for_catalog(updates, eligible_car_ids)
            selected = {
                car_id: update
                for car_id, update in selected.items()
                if car_id not in completed_before_run
                and car_id not in completed_during_run
            }

            if selected:
                completed_updates = {
                    car_id: update.completion_reason
                    for car_id, update in selected.items()
                    if car_id in active_open_ids
                    and update.completion_reason is not None
                }

                selected = {
                    car_id: (
                        update
                        if car_id in active_open_ids
                        else replace(
                            update,
                            highest_offer=None,
                            user_offer=None,
                            bid_status=None,
                            auction_phase=None,
                            final_call_active=False,
                            remaining_seconds=None,
                            auction_end_at=None,
                            live_bidding=None,
                        )
                    )
                    for car_id, update in selected.items()
                }

                summary = store_car_updates(
                    self.database,
                    selected,
                    auction,
                    observed_at=captured.observed_at,
                    lot_metadata={
                        car_id: lot_metadata[car_id]
                        for car_id in selected
                        if car_id in lot_metadata
                    },
                )
                for car_id, reason in completed_updates.items():
                    completed = self.database.mark_open_auction_completed(
                        car_id,
                        auction.exleasingcar_auction_id,
                        reason=reason,
                        completed_at=captured.observed_at,
                    )
                    if completed:
                        completed_during_run.add(car_id)
                        active_open_ids.discard(car_id)
                        print(
                            f"  Lot {car_id} bidding ended; "
                            "final observation saved. Live monitoring stopped for this lot."
                        )

                self.api_cars_processed += summary.cars_processed
                self.prices_checked += summary.prices_checked
                self.prices_inserted += summary.prices_inserted
                self.prices_unchanged += summary.prices_unchanged
                self.price_references_checked += summary.price_references_checked
                self.price_references_inserted += summary.price_references_inserted
                self.price_references_unchanged += summary.price_references_unchanged
                self.events_checked += summary.events_checked
                self.events_inserted += summary.events_inserted
                self.events_unchanged += summary.events_unchanged

            # Advance only after the database observations have committed. If
            # this write fails, the exact response is replayed idempotently.
            if deferred_update_id is not None:
                self.database.mark_catalog_api_lot_update_processed(
                    deferred_update_id,
                    observed_at=datetime.now(timezone.utc),
                )
            elif page_id is not None and response_index is not None:
                self.database.advance_catalog_api_response_cursor(
                    page_id, response_index
                )
            else:
                self.api_cursor += 1
            return True

        pending_pages = self.database.get_pending_catalog_api_pages(
            auction.exleasingcar_auction_id
        )
        if pending_pages:
            # Durable queues take precedence over in-memory capture, including
            # responses left behind by a previous interrupted collector run.
            for stored_page in pending_pages:
                for response_index in range(stored_page.api_responses_processed,
                                            len(stored_page.api_responses)):
                    stored_response = stored_page.api_responses[response_index]
                    captured = CapturedApiResponse(
                        url=str(stored_response["url"]),
                        payload=stored_response["payload"],
                        observed_at=_restore_catalog_page_datetime(
                            str(stored_response["observed_at"])
                        ),
                    )
                    if not process_response(
                        captured,
                        page_id=stored_page.page_id,
                        response_index=response_index,
                    ):
                        return
        else:
            # Keep the direct recorder path for isolated parser/ingestion tests
            # and older callers that do not have a persisted scan checkpoint.
            while self.api_cursor < len(self.recorder.responses):
                captured = self.recorder.responses[self.api_cursor]
                if not process_response(
                    captured,
                    page_id=None,
                    response_index=None,
                ):
                    return

        # Unknown sale modes no longer hold the bulk API cursor in place.
        # Once detail enrichment classifies one of those lots, replay its
        # individually retained public payload.
        for deferred in self.database.get_pending_catalog_api_lot_updates(
            auction.exleasingcar_auction_id
        ):
            captured = CapturedApiResponse(
                url=str(deferred["url"]),
                payload=deferred["payload"],
                observed_at=_restore_catalog_page_datetime(
                    str(deferred["observed_at"])
                ),
            )
            if not process_response(
                captured,
                page_id=None,
                response_index=None,
                deferred_update_id=int(deferred["id"]),
            ):
                return

    def _persist_catalog_lot_observations(
        self,
        auction: AuctionContext,
        catalog_lots: Mapping[str, tuple[ParsedLot, datetime]],
        open_auction_car_ids: set[str],
        *,
        completed_before_run: set[str],
    ) -> set[str]:
        """Save public catalogue prices and source-confirmed lot completion states."""
        completed_by_catalog: set[str] = set()
        for car_id, (lot, observed_at) in catalog_lots.items():
            if car_id not in open_auction_car_ids or car_id in completed_before_run:
                continue

            if lot.current_price is not None and lot.current_price.amount > 0:
                displayed_price = PriceObservation(
                    price_type=PriceType.DISPLAYED_AUCTION_PRICE,
                    amount=lot.current_price.amount,
                    currency=lot.current_price.currency,
                    raw_text=lot.current_price.raw_text,
                    source_field=lot.current_price.source_field,
                )
                inserted = self.database.record_lot_price_observation(
                    car_id,
                    displayed_price,
                    observed_at=observed_at,
                    source="auction_catalog_html",
                    exleasingcar_auction_id=auction.exleasingcar_auction_id,
                )
                self.lot_price_observations_checked += 1
                self.lot_price_observations_inserted += int(inserted)
                self.lot_price_observations_unchanged += int(not inserted)

            if lot.status is VehicleStatus.COMPLETED:
                self.database.record_auction_event(
                    car_id,
                    bid_status="completed",
                    observed_at=observed_at,
                    source="auction_catalog_html",
                    exleasingcar_auction_id=auction.exleasingcar_auction_id,
                )
                self.database.mark_open_auction_completed(
                    car_id,
                    auction.exleasingcar_auction_id,
                    reason="catalog_terminal_status",
                    completed_at=observed_at,
                )
                completed_by_catalog.add(car_id)
                print(f"  Lot {car_id} is marked as finished in the auction catalogue.")

        return completed_by_catalog

    def _qualify_open_candidates_from_catalog(
        self,
        auction: AuctionContext,
        catalog_lots: Mapping[str, tuple[ParsedLot, datetime]],
        candidate_open_ids: set[str],
        eligible_car_ids: set[str],
        open_auction_car_ids: set[str],
    ) -> None:
        """Qualify known open lots when the catalogue shows a positive price."""
        for car_id, (lot, observed_at) in catalog_lots.items():
            if (
                car_id not in candidate_open_ids
                or car_id in eligible_car_ids
                or lot.current_price is None
                or lot.current_price.amount <= 0
            ):
                continue
            promoted = self.database.set_auction_lot_qualification(
                car_id,
                auction.exleasingcar_auction_id,
                sale_type=SaleType.OPEN_AUCTION,
                has_fixed_price=False,
                collection_eligible=True,
                checked_at=observed_at,
            )
            if not promoted:
                continue
            eligible_car_ids.add(car_id)
            open_auction_car_ids.add(car_id)
            self.qualified_lots += 1
            print(
                f"  Open lot {car_id} qualified by its public catalogue price."
            )

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
                collection_eligible=None,
                observed_at=observed_at,
            )
            result[lot.source_car_id] = _lot_metadata(lot)
        return result

    @staticmethod
    def _page_body_text(page: Any) -> str:
        try:
            return page.locator("body").inner_text(timeout=5_000)
        except PlaywrightError:
            return ""

    @classmethod
    def _is_logged_in(cls, page: Any) -> bool:
        """Detect the authenticated account header without relying on a user name."""
        body_text = cls._page_body_text(page)
        return bool(
            re.search(
                (
                r"ID\s+(?:вашей\s+)?учетной\s+записи|"
                r"\bLog\s*out\b|\bSign\s*out\b|Выйти"
            ),
                body_text,
                re.IGNORECASE,
            )
        )

    @staticmethod
    def _first_visible(locator: Any) -> Any | None:
        """Return the first visible element in a Playwright locator, if any."""
        try:
            for index in range(locator.count()):
                candidate = locator.nth(index)
                if candidate.is_visible():
                    return candidate
        except PlaywrightError:
            return None
        return None

    @classmethod
    def _dismiss_notification_prompt(cls, page: Any) -> bool:
        """Dismiss the site's optional OneSignal prompt without granting permission."""
        container = cls._first_visible(
            page.locator("#onesignal-slidedown-container")
        )
        if container is None:
            return False

        dismiss_pattern = re.compile(
            r"^\s*(?:NO\s+THANKS|NOT\s+NOW|НЕТ,?\s*СПАСИБО|НЕ\s+СЕЙЧАС)\s*$",
            re.IGNORECASE,
        )
        candidates = (
            container.get_by_role("button", name=dismiss_pattern),
            container.locator(
                "#onesignal-slidedown-cancel-button, "
                ".onesignal-slidedown-cancel-button, "
                "button[class*='cancel'], a[class*='cancel']"
            ),
            container.get_by_text(dismiss_pattern),
        )
        dismiss_button = None
        for locator in candidates:
            dismiss_button = cls._first_visible(locator)
            if dismiss_button is not None:
                break

        if dismiss_button is None:
            raise RuntimeError(
                "The website notification prompt is blocking the page, but its "
                "'NO THANKS' control was not found. Close the prompt manually; "
                "the collector will not enable notifications."
            )

        dismiss_button.click(timeout=5_000)
        try:
            container.wait_for(state="hidden", timeout=3_000)
        except PlaywrightError as exc:
            raise RuntimeError(
                "The 'NO THANKS' button was clicked, but the website notification "
                "prompt is still visible. Inspect the Chromium window."
            ) from exc

        print("Website notification prompt dismissed (NO THANKS).")
        return True

    @classmethod
    def _click_visible_text(
        cls,
        page: Any,
        pattern: re.Pattern[str],
        *,
        roles: tuple[str, ...] = ("button", "link"),
    ) -> bool:
        """Click the first visible button/link/text matching a known UI label."""
        cls._dismiss_notification_prompt(page)
        for role in roles:
            try:
                candidate = cls._first_visible(
                    page.get_by_role(role, name=pattern)
                )
            except PlaywrightError:
                candidate = None
            if candidate is not None:
                candidate.click(timeout=8_000)
                return True

        try:
            candidate = cls._first_visible(page.get_by_text(pattern))
        except PlaywrightError:
            candidate = None
        if candidate is not None:
            candidate.click(timeout=8_000)
            return True
        return False

    @classmethod
    def _find_username_input(cls, page: Any, password_input: Any) -> Any | None:
        """Find the username field in the same form as the password field."""
        scope = page
        try:
            form = password_input.locator("xpath=ancestor::form[1]")
            if form.count() and form.first.is_visible():
                scope = form.first
        except PlaywrightError:
            scope = page

        preferred_selectors = (
            'input[autocomplete="username"]:visible',
            'input[type="email"]:visible',
            'input[name*="email" i]:visible',
            'input[id*="email" i]:visible',
            'input[name*="login" i]:visible',
            'input[id*="login" i]:visible',
            'input[name*="user" i]:visible',
        )
        for selector in preferred_selectors:
            try:
                candidate = cls._first_visible(scope.locator(selector))
            except PlaywrightError:
                candidate = None
            if candidate is not None:
                return candidate

        # Some older Exleasingcar forms use a plain text input without semantic
        # attributes. Exclude the vehicle-ID search field and password inputs.
        try:
            candidates = scope.locator('input[type="text"]:visible')
            for index in range(candidates.count()):
                candidate = candidates.nth(index)
                attrs = " ".join(
                    str(candidate.get_attribute(name) or "")
                    for name in ("name", "id", "placeholder", "autocomplete")
                ).casefold()
                if "автомобил" in attrs or "vehicle" in attrs or "search" in attrs:
                    continue
                return candidate
        except PlaywrightError:
            return None
        return None

    @classmethod
    def _click_continue_if_visible(cls, page: Any) -> bool:
        """Acknowledge the site's post-login information dialog if it appears."""
        continue_pattern = re.compile(r"^\s*Продолжить\b", re.IGNORECASE)
        clicked = cls._click_visible_text(page, continue_pattern)
        if clicked:
            page.wait_for_timeout(1_500)
        return clicked

    def _load_login_credentials(self) -> tuple[str, str]:
        try:
            import keyring
        except ImportError as exc:
            raise RuntimeError(
                "The keyring package is missing. Activate .venv and run: "
                "python -m pip install keyring"
            ) from exc

        username = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME_KEY)
        password = keyring.get_password(KEYRING_SERVICE, KEYRING_PASSWORD_KEY)
        if not username or not password:
            raise RuntimeError(
                "Exleasingcar credentials were not found in Windows Credential Manager. "
                "Save them first using the instructions in README_AUTO_LOGIN.md."
            )
        return username, password

    def _authenticate(self, page: Any, *, reason: str) -> bool:
        """Perform the ordinary site login and its visible Continue acknowledgement."""
        if self._is_logged_in(page):
            return True

        print(f"Authentication required: {reason}")
        password_inputs = page.locator('input[type="password"]:visible')
        password_input = self._first_visible(password_inputs)

        if password_input is None:
            login_pattern = re.compile(r"^\s*Войти\s*$", re.IGNORECASE)
            if not self._click_visible_text(page, login_pattern):
                raise RuntimeError(
                    "The account is signed out, but the visible 'Войти' control "
                    "was not found. The current page may have changed; inspect "
                    f"the Chromium window. URL: {page.url}"
                )
            try:
                page.wait_for_selector(
                    'input[type="password"]:visible',
                    state="visible",
                    timeout=12_000,
                )
            except PlaywrightError as exc:
                raise RuntimeError(
                    "The login button was clicked, but the password form did not appear. "
                    f"Current URL: {page.url}"
                ) from exc
            password_input = self._first_visible(
                page.locator('input[type="password"]:visible')
            )

        if password_input is None:
            raise RuntimeError("The visible password field could not be located.")

        username, password = self._load_login_credentials()
        username_input = self._find_username_input(page, password_input)
        if username_input is None:
            visible_inputs: list[str] = []
            try:
                inputs = page.locator("input:visible")
                for index in range(inputs.count()):
                    item = inputs.nth(index)
                    visible_inputs.append(
                        "type={}; name={}; id={}; placeholder={}; autocomplete={}".format(
                            item.get_attribute("type"),
                            item.get_attribute("name"),
                            item.get_attribute("id"),
                            item.get_attribute("placeholder"),
                            item.get_attribute("autocomplete"),
                        )
                    )
            except PlaywrightError:
                pass
            raise RuntimeError(
                "The password field is visible, but the username/email field was "
                "not identified. Visible input metadata (no values): "
                + " | ".join(visible_inputs)
            )

        username_input.fill(username)
        password_input.fill(password)

        login_submit = re.compile(r"Войти\s+на\s+аукционы", re.IGNORECASE)
        if not self._click_visible_text(page, login_submit, roles=("button",)):
            # Some versions render the yellow form action as an input submit.
            submit = self._first_visible(
                page.locator('input[type="submit"][value*="Войти" i]:visible')
            )
            if submit is None:
                raise RuntimeError(
                    "Credentials were filled, but the 'Войти на аукционы' button "
                    "was not found. No submit action was performed."
                )
            submit.click(timeout=8_000)

        page.wait_for_timeout(2_000)
        self._click_continue_if_visible(page)

        if not self._is_logged_in(page):
            body_text = self._page_body_text(page)
            verification_requested = re.search(
                r"captcha|recaptcha|подтвердите, что вы не робот",
                body_text,
                re.IGNORECASE,
            )
            if verification_requested:
                raise RuntimeError(
                    "The site requested an additional CAPTCHA/verification step. "
                    "Complete it manually in Chromium; the collector will not bypass it."
                )
            raise RuntimeError(
                "The login form was submitted, but the authenticated account header "
                "was not detected. Check whether the site requires additional "
                "account/document verification."
            )

        print("Exleasingcar account session confirmed.")
        return True

    def _pause_for_manual_reauthentication(
        self,
        page: Any,
        detail_url: str,
        car_id: str,
    ) -> bool:
        """Legacy manual retry hook retained for compatibility with older tests."""
        if self._manual_reauth_prompted:
            return False
        self._manual_reauth_prompted = True
        input(
            f"Sign in manually for vehicle {car_id}, then press Enter to retry: "
        )
        try:
            page.goto(detail_url, wait_until="domcontentloaded", timeout=45_000)
            _wait_for_dynamic_content(page)
            self.recorder.wait_for_pending(page)
        except PlaywrightError as exc:
            print(f"  Retry after manual sign-in failed for {car_id}: {exc}")
            return False
        return True

    def _enrich_vehicle_detail(
        self,
        auction: AuctionContext,
        car_id: str,
        detail_url: str,
        page: Any,
        observed_at: datetime,
        snapshot_dir: Path,
    ) -> str | None:
        """Qualify a lot before retaining detail fields, media links or prices."""
        try:
            page.goto(detail_url, wait_until="domcontentloaded", timeout=45_000)
            _wait_for_dynamic_content(page)
            self.recorder.wait_for_pending(page, timeout_ms=10_000)
        except PlaywrightError as exc:
            print(f"  Detail page {car_id} failed: {exc}")
            return None

        html = page.content()
        vehicle = parse_vehicle_metadata(html, page.url)
        if vehicle.source_car_id != car_id and not self._is_logged_in(page):
            self._authenticate(
                page,
                reason=(
                    f"vehicle {car_id} redirected to {page.url}; the account session "
                    "appears to have expired"
                ),
            )
            try:
                page.goto(detail_url, wait_until="domcontentloaded", timeout=45_000)
                _wait_for_dynamic_content(page)
                self.recorder.wait_for_pending(page, timeout_ms=10_000)
            except PlaywrightError as exc:
                print(f"  Detail retry after automatic login failed for {car_id}: {exc}")
                return None
            html = page.content()
            vehicle = parse_vehicle_metadata(html, page.url)
        elif (
            vehicle.source_car_id != car_id
            and self._click_continue_if_visible(page)
        ):
            try:
                page.goto(detail_url, wait_until="domcontentloaded", timeout=45_000)
                _wait_for_dynamic_content(page)
                self.recorder.wait_for_pending(page, timeout_ms=10_000)
            except PlaywrightError as exc:
                print(f"  Detail retry after Continue failed for {car_id}: {exc}")
                return None
            html = page.content()
            vehicle = parse_vehicle_metadata(html, page.url)

        if vehicle.source_car_id != car_id:
            print(
                f"  Skipped detail {car_id}: expected page ID {car_id}, "
                f"received {vehicle.source_car_id!r}; final URL: {page.url}; "
                f"title: {page.title()!r}."
            )
            return None

        self._manual_reauth_prompted = False
        if (
            vehicle.exleasingcar_auction_id is not None
            and vehicle.exleasingcar_auction_id != auction.exleasingcar_auction_id
        ):
            print(
                f"  Skipped detail {car_id}: auction ID mismatch "
                f"{vehicle.exleasingcar_auction_id} != {auction.exleasingcar_auction_id}"
            )
            return None

        parsed_detail = parse_vehicle_detail(
            html,
            source_car_id=car_id,
            page_url=page.url,
        )
        fixed_price_available = (
            parsed_detail.fixed_price is not None
            and parsed_detail.fixed_price.amount > 0
        )
        previously_eligible = self.database.get_collection_eligible_lots(
            auction.exleasingcar_auction_id,
            {car_id},
        )
        current_sale_type = (
            parsed_detail.sale_type.value
            if parsed_detail.sale_type is not None
            else None
        )
        archive_eligible = parsed_detail.should_archive or (
            car_id in previously_eligible
            and previously_eligible[car_id] == current_sale_type
        )
        self.database.set_auction_lot_qualification(
            car_id,
            auction.exleasingcar_auction_id,
            sale_type=parsed_detail.sale_type,
            has_fixed_price=fixed_price_available,
            collection_eligible=archive_eligible,
            checked_at=datetime.now(timezone.utc),
        )

        if parsed_detail.sale_type is not None:
            self.sale_types_recognized += 1
        if fixed_price_available:
            self.lots_with_fixed_price += 1

        if not archive_eligible:
            if parsed_detail.sale_type is SaleType.CLOSED_AUCTION:
                self.excluded_closed_lots += 1
                outcome = "excluded_closed"
                reason = "closed bidding without a verified fixed price"
            elif parsed_detail.sale_type is SaleType.OPEN_AUCTION:
                self.excluded_unpriced_lots += 1
                outcome = "excluded_unpriced"
                reason = (
                    "open auction has no positive displayed price or public bid yet"
                )
            else:
                self.excluded_unknown_lots += 1
                outcome = "excluded_unknown"
                reason = "unknown sale mode and no verified fixed price"
            print(
                f"  Lot {car_id} excluded from the archive: {reason}; "
                "no detail metadata, price history or media URLs retained."
            )
            return outcome

        self.qualified_lots += 1
        self._save_html(snapshot_dir / f"vehicle_{_safe_component(car_id)}.html", html)
        media_references = parse_vehicle_media(html, page.url)
        for reference in media_references:
            self.database.upsert_lot_media_source(
                car_id,
                auction.exleasingcar_auction_id,
                reference.media_type,
                reference.source_url,
                preview_url=reference.preview_url,
                label=reference.label,
                source_order=reference.source_order,
                discovered_at=observed_at,
            )
        if media_references:
            photo_count = sum(
                reference.media_type == "photo" for reference in media_references
            )
            report_count = len(media_references) - photo_count
            print(
                f"  Media references for {car_id}: "
                f"{photo_count} photo(s), {report_count} report(s)"
            )

        if parsed_detail.sale_type is None:
            print(
                f"  Lot {car_id} included only because a verified fixed price "
                "is publicly available; bidding mode remains unknown."
            )

        for price in (
            parsed_detail.displayed_auction_price,
            parsed_detail.fixed_price,
        ):
            if price is None or price.amount <= 0:
                continue
            inserted = self.database.record_lot_price_observation(
                car_id,
                price,
                observed_at=observed_at,
                exleasingcar_auction_id=auction.exleasingcar_auction_id,
                sale_type=parsed_detail.sale_type,
            )
            self.lot_price_observations_checked += 1
            self.lot_price_observations_inserted += int(inserted)
            self.lot_price_observations_unchanged += int(not inserted)

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

        if parsed_detail.sale_type is SaleType.OPEN_AUCTION:
            return "open_auction"
        return "fixed_price"

    def _discover_auction_pages(
        self,
        page: Any,
        *,
        initial_page_url: str,
        initial_html: str,
        snapshot_dir: Path,
        run_budget: RunBudget,
        force: bool,
    ) -> tuple[Any, int]:
        """Discover and checkpoint every auction-list page before bootstrap work."""
        progress = self.database.start_or_resume_auction_discovery(
            initial_page_url,
            force=force,
            refresh_after_seconds=24 * 60 * 60,
            observed_at=datetime.now(timezone.utc),
        )
        if progress.status == "completed":
            print(
                "Auction discovery is already complete; "
                f"{progress.pages_completed} list page(s) are checkpointed."
            )
            return progress, 0

        saved_pages = self.database.get_auction_discovery_pages(progress.run_id)
        visited_urls = {item.page_url for item in saved_pages}
        current_url = progress.next_page_url
        page_number = progress.next_page_number
        current_html = (
            initial_html
            if current_url == initial_page_url and page.url == initial_page_url
            else None
        )
        pages_processed = 0

        while current_url is not None and page_number is not None:
            blocked = run_budget.try_consume(catalog_pages=1)
            if blocked is not None:
                print(
                    "  Auction discovery paused before list page "
                    f"{page_number}: {blocked.value}."
                )
                break

            if current_html is None:
                try:
                    page.goto(
                        current_url,
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                    _wait_for_dynamic_content(page)
                    if not self.recorder.wait_for_pending(page):
                        print(
                            "  Auction-list page left uncheckpointed because "
                            "its pending requests did not finish."
                        )
                        break
                    current_html = page.content()
                except PlaywrightError as exc:
                    print(
                        "  Auction-list page navigation failed; "
                        f"page {page_number} remains resumable: {exc}"
                    )
                    break

            page_base_url = page.url or current_url
            listings = parse_auction_listings(current_html, page_base_url)
            if not listings:
                raise RuntimeError(
                    "Auction discovery found no auction cards on list page "
                    f"{page_number}; the page was not checkpointed."
                )

            pagination = parse_catalog_pagination(current_html, page_base_url)
            next_url = pagination.next_page_url
            if next_url is None:
                future_candidates = [
                    candidate
                    for candidate in pagination.page_urls
                    if candidate not in visited_urls
                    and candidate not in {current_url, page_base_url}
                ]
                next_url = future_candidates[0] if future_candidates else None

            if next_url is not None and (
                next_url in visited_urls or next_url in {current_url, page_base_url}
            ):
                raise RuntimeError(
                    "Auction-list pagination points to an already visited page; "
                    "discovery remains incomplete."
                )

            observed_at = datetime.now(timezone.utc)
            for listing in listings:
                self.database.upsert_auction(
                    listing.exleasingcar_auction_id,
                    source_auction_name=listing.source_auction_name,
                    source_auction_country_code=listing.source_auction_country_code,
                    source_auction_country_name=listing.source_auction_country_name,
                    auction_type_label=listing.auction_type_label,
                    auction_status_label=listing.auction_status_label,
                    source_url=listing.source_url,
                    markers=listing.markers,
                    observed_at=observed_at,
                )
                self.database.enqueue_bootstrap_auction(
                    listing.exleasingcar_auction_id,
                    observed_at=observed_at,
                )
                if not listing.source_url:
                    print(
                        f"  Auction {listing.exleasingcar_auction_id} is recorded "
                        "but blocked from bootstrap: no navigable source URL."
                    )

            next_page_number = (
                (pagination.current_page_number or page_number) + 1
                if next_url is not None
                else None
            )
            self.database.record_auction_discovery_page(
                progress.run_id,
                page_number,
                current_url,
                next_page_url=next_url,
                next_page_number=next_page_number,
                auction_count=len(listings),
                observed_at=observed_at,
            )
            pages_processed += 1
            visited_urls.add(current_url)
            print(
                f"  Auction list page {page_number}: "
                f"{len(listings)} card(s) checkpointed."
            )

            # List-page API traffic is deliberately not attributed to any one
            # auction. Keep memory bounded; per-auction API capture starts fresh.
            self.recorder.responses.clear()
            self.recorder.errors.clear()
            self.api_cursor = 0

            if next_url is None:
                current_url = None
                page_number = None
            else:
                current_url = next_url
                page_number = next_page_number
                current_html = None
                if self.page_delay_seconds:
                    time.sleep(self.page_delay_seconds)

        latest = self.database.get_latest_auction_discovery_progress()
        if latest is None:
            raise RuntimeError("Auction discovery state disappeared unexpectedly.")
        return latest, pages_processed

    @staticmethod
    def _bootstrap_listing(item: BootstrapQueueItem) -> ParsedAuctionListing:
        """Rehydrate a source listing from the persistent bootstrap queue."""
        return ParsedAuctionListing(
            exleasingcar_auction_id=item.auction_id,
            source_auction_name=item.source_auction_name,
            source_auction_country_code=item.source_auction_country_code,
            source_auction_country_name=item.source_auction_country_name,
            auction_type_label=item.auction_type_label,
            auction_status_label=item.auction_status_label,
            vehicle_count=None,
            source_url=item.source_url,
            markers=item.markers,
        )

    def _collect_auction(
        self,
        listing: ParsedAuctionListing,
        page: Any,
        snapshot_dir: Path,
        *,
        max_detail_pages: int,
        run_budget: RunBudget | None = None,
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
            markers=listing.markers,
        )
        auction_dir = snapshot_dir / f"auction_{_safe_component(listing.exleasingcar_auction_id)}"
        auction_dir.mkdir(parents=True, exist_ok=True)

        scan_type = (
            "bootstrap"
            if not self.database.has_completed_catalog_scan(
                listing.exleasingcar_auction_id,
                "bootstrap",
            )
            else "routine"
        )
        scan = self.database.start_or_resume_catalog_scan(
            listing.exleasingcar_auction_id,
            scan_type,
            listing.source_url,
            observed_at=datetime.now(timezone.utc),
        )

        pages_processed = 0
        details_processed = 0
        details_attempted = 0
        failed_detail_ids: list[str] = []
        attempted_detail_ids: set[str] = set()
        observed_at = datetime.now(timezone.utc)

        # A resumed run must remember URLs that have already been checkpointed,
        # otherwise fallback pagination links could send it back to page one.
        visited_urls = {
            saved_page.page_url
            for saved_page in self.database.get_catalog_scan_pages(scan.scan_id)
        }
        current_url = scan.next_page_url
        page_number = scan.next_page_number

        while current_url is not None and page_number is not None:
            if run_budget is not None:
                blocked = run_budget.try_consume(catalog_pages=1)
                if blocked is not None:
                    print(
                        f"  Catalogue scan paused before page {page_number}: "
                        f"{blocked.value}."
                    )
                    break

            response_start = len(self.recorder.responses)
            error_start = len(self.recorder.errors)
            try:
                navigation_response = page.goto(
                    current_url,
                    wait_until="domcontentloaded",
                    timeout=45_000,
                )
                _wait_for_dynamic_content(page)
                if not self.recorder.wait_for_pending(page):
                    print(
                        "  Catalogue page left uncheckpointed because its API "
                        "response(s) did not finish before the capture deadline."
                    )
                    break
            except PlaywrightError as exc:
                print(f"  Catalogue page failed ({current_url}): {exc}")
                break

            if len(self.recorder.errors) > error_start:
                print(
                    "  Catalogue page left uncheckpointed because an API response "
                    "could not be captured safely."
                )
                break

            html = page.content()
            page_observed_at = datetime.now(timezone.utc)
            final_url = getattr(page, "url", None) or current_url
            catalog = parse_auction_catalog_metadata(html, final_url)
            page_status = parse_auction_list(html, final_url)
            if catalog.exleasingcar_auction_id != listing.exleasingcar_auction_id:
                auction_cards = parse_auction_listings(html, final_url)
                page_kind = "auction_list" if auction_cards else "unknown"
                response_status = (
                    getattr(navigation_response, "status", None)
                    if navigation_response is not None
                    else None
                )
                response_url = (
                    getattr(navigation_response, "url", None)
                    if navigation_response is not None
                    else None
                )
                diagnostic = {
                    "expected_auction_id": listing.exleasingcar_auction_id,
                    "received_auction_id": catalog.exleasingcar_auction_id,
                    "page_kind": page_kind,
                    "auction_cards_found": len(auction_cards),
                    "requested_url": _safe_diagnostic_url(current_url),
                    "final_url": _safe_diagnostic_url(final_url),
                    "response_url": _safe_diagnostic_url(response_url),
                    "http_status": response_status,
                    "observed_at": page_observed_at.isoformat(),
                }
                self._save_html(
                    auction_dir / f"unexpected_catalogue_page_{page_number:03d}.html",
                    html,
                )
                (auction_dir / f"catalogue_mismatch_page_{page_number:03d}.json").write_text(
                    json.dumps(diagnostic, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                raise CataloguePageMismatchError(
                    listing.exleasingcar_auction_id,
                    catalog.exleasingcar_auction_id,
                    page_kind,
                )

            self._save_html(
                auction_dir / f"catalog_page_{page_number:03d}.html",
                html,
            )

            # Persist parsed lot rows before committing the page cursor. If the
            # process stops here, the page is replayed and these upserts are safe.
            self._upsert_catalog_lots(auction, catalog.lots, page_observed_at)
            page_detail_urls = extract_vehicle_detail_urls(html, page.url)
            pagination = parse_catalog_pagination(html, page.url)
            next_url = _next_unvisited_catalog_page_url(
                pagination,
                current_page_url=current_url,
                visited_page_urls=visited_urls,
            )

            next_number = page_number + 1 if next_url is not None else None
            page_data = _serialize_catalog_page_data(
                catalog.lots,
                page_status.lots,
                page_detail_urls,
                page_observed_at,
            )
            captured_responses = self.recorder.responses[response_start:]
            api_response_rows = tuple(
                {
                    "url": captured.url,
                    "payload": captured.payload,
                    "observed_at": captured.observed_at,
                }
                for captured in captured_responses
            )

            # Page metadata, parsed lot/detail references, captured API payloads,
            # and the next-page cursor are committed atomically in SQLite.
            self.database.record_catalog_page_completed(
                scan.scan_id,
                page_number,
                current_url,
                next_page_url=next_url,
                next_page_number=next_number,
                item_count=len(catalog.lots),
                page_data=page_data,
                api_responses=api_response_rows,
                observed_at=page_observed_at,
            )
            visited_urls.add(current_url)
            pages_processed += 1
            current_url = next_url
            page_number = next_number

            if self.page_delay_seconds:
                time.sleep(self.page_delay_seconds)

        # All page-scoped responses are either checkpointed in SQLite or
        # discarded with an incomplete page. Never let an in-memory response
        # from one auction fall through to the next auction's API processing.
        self.api_cursor = max(self.api_cursor, len(self.recorder.responses))
        progress = self.database.get_catalog_scan_progress(scan.scan_id)
        if (
            progress is not None
            and progress.status == "in_progress"
            and progress.next_page_url is None
            and progress.next_page_number is None
            and progress.pages_completed > 0
        ):
            self.database.complete_catalog_scan(
                scan.scan_id,
                observed_at=datetime.now(timezone.utc),
            )
            progress = self.database.get_catalog_scan_progress(scan.scan_id)

        # Rebuild every in-memory work set from the durable page payloads. This
        # is what makes detail URLs and status/price observations survive a
        # process restart between catalogue pages.
        all_lot_metadata: dict[str, LotMetadata] = {}
        catalog_lot_observations: dict[str, tuple[ParsedLot, datetime]] = {}
        detail_urls: dict[str, str] = {}
        lot_page_ids: dict[str, int] = {}
        metadata_page_ranks: dict[str, tuple[datetime, int]] = {}
        detail_page_ranks: dict[str, tuple[datetime, int]] = {}
        default_page_rank = (datetime.min.replace(tzinfo=timezone.utc), 0)

        def merge_restored_page_data(
            page_id: int,
            page_metadata: tuple[ParsedAuctionLotMetadata, ...],
            parsed_lots: tuple[ParsedLot, ...],
            saved_detail_urls: Mapping[str, str],
            page_observed_at: datetime,
        ) -> None:
            page_rank = (page_observed_at, page_id)
            for item in page_metadata:
                car_id = item.source_car_id
                if page_rank > metadata_page_ranks.get(car_id, default_page_rank):
                    newest = _lot_metadata(item)
                    previous = all_lot_metadata.get(car_id)
                    if previous is not None:
                        newest = LotMetadata(
                            displayed_lot_reference=(
                                newest.displayed_lot_reference
                                or previous.displayed_lot_reference
                            ),
                            listing_code_attribute=(
                                newest.listing_code_attribute
                                or previous.listing_code_attribute
                            ),
                            mileage_km=(
                                newest.mileage_km
                                if newest.mileage_km is not None
                                else previous.mileage_km
                            ),
                        )
                    all_lot_metadata[car_id] = newest
                    metadata_page_ranks[car_id] = page_rank

            for car_id, detail_url in saved_detail_urls.items():
                if page_rank > detail_page_ranks.get(car_id, default_page_rank):
                    detail_urls[car_id] = detail_url
                    lot_page_ids[car_id] = page_id
                    detail_page_ranks[car_id] = page_rank

            for item in parsed_lots:
                _merge_catalog_lot_observation(
                    catalog_lot_observations, item, page_observed_at
                )

        lots_seen = 0
        saved_pages = self.database.get_catalog_scan_pages(scan.scan_id)
        restored_page_ids = {saved_page.page_id for saved_page in saved_pages}
        for saved_page in saved_pages:
            (
                page_metadata,
                parsed_lots,
                saved_detail_urls,
                page_observed_at,
                page_lots_seen,
            ) = _restore_catalog_page_data(
                saved_page.page_data,
                fallback_observed_at=saved_page.completed_at,
            )
            merge_restored_page_data(
                saved_page.page_id,
                page_metadata,
                parsed_lots,
                saved_detail_urls,
                page_observed_at,
            )
            lots_seen += page_lots_seen

        # Older unfinished API queues may belong to a previous scan generation.
        # Rehydrate those page references too so deferred snapshots are not lost
        # merely because a lot disappeared from the latest catalogue.
        for pending_page in self.database.get_pending_catalog_api_pages(
            listing.exleasingcar_auction_id
        ):
            if pending_page.page_id in restored_page_ids:
                continue
            restored_page_ids.add(pending_page.page_id)
            (
                page_metadata,
                parsed_lots,
                saved_detail_urls,
                page_observed_at,
                page_lots_seen,
            ) = _restore_catalog_page_data(
                pending_page.page_data,
                fallback_observed_at=pending_page.completed_at,
            )
            merge_restored_page_data(
                pending_page.page_id,
                page_metadata,
                parsed_lots,
                saved_detail_urls,
                page_observed_at,
            )
            lots_seen += page_lots_seen

        # Deferred API lot rows can outlive their original bulk-response cursor.
        # Rehydrate their old page metadata and detail URLs even when those lots
        # no longer appear in the latest catalogue listing.
        for deferred_page in self.database.get_catalog_deferred_api_pages(
            listing.exleasingcar_auction_id
        ):
            if deferred_page.page_id in restored_page_ids:
                continue
            restored_page_ids.add(deferred_page.page_id)
            (
                page_metadata,
                parsed_lots,
                saved_detail_urls,
                page_observed_at,
                page_lots_seen,
            ) = _restore_catalog_page_data(
                deferred_page.page_data,
                fallback_observed_at=deferred_page.completed_at,
            )
            merge_restored_page_data(
                deferred_page.page_id,
                page_metadata,
                parsed_lots,
                saved_detail_urls,
                page_observed_at,
            )
            lots_seen += page_lots_seen

        if (
            progress is not None
            and progress.status == "completed"
            and listing.vehicle_count is not None
            and listing.vehicle_count > len(all_lot_metadata)
        ):
            print(
                f"  WARNING: auction list reports {listing.vehicle_count} vehicles, "
                f"but only {len(all_lot_metadata)} unique lot IDs were found. "
                "The completed catalogue scan may still be incomplete."
            )

        previously_eligible = self.database.get_collection_eligible_lots(
            listing.exleasingcar_auction_id,
            set(all_lot_metadata),
        )
        eligible_car_ids = set(previously_eligible)
        open_auction_car_ids = {
            car_id
            for car_id, sale_type in previously_eligible.items()
            if sale_type == SaleType.OPEN_AUCTION.value
        }
        open_auction_candidate_ids = self.database.get_open_auction_lot_ids(
            listing.exleasingcar_auction_id,
            set(all_lot_metadata),
        )

        # Qualify catalogue-price candidates and persist public API snapshots
        # before terminal catalogue states. This preserves the final bid/price
        # captured for the same page, then prevents completed lots from detail
        # rotation below.
        self._qualify_open_candidates_from_catalog(
            auction,
            catalog_lot_observations,
            open_auction_candidate_ids,
            eligible_car_ids,
            open_auction_car_ids,
        )
        self._persist_api_updates(
            auction,
            eligible_car_ids,
            all_lot_metadata,
            open_auction_car_ids=open_auction_car_ids,
            open_auction_candidate_ids=open_auction_candidate_ids,
            run_budget=run_budget,
        )
        completed_before_detail = self.database.get_completed_open_auction_lots(
            listing.exleasingcar_auction_id,
            eligible_car_ids,
        )
        completed_before_detail.update(
            self._persist_catalog_lot_observations(
                auction,
                catalog_lot_observations,
                open_auction_car_ids,
                completed_before_run=completed_before_detail,
            )
        )

        ordered_car_ids = self.database.get_detail_check_order(
            listing.exleasingcar_auction_id,
            set(detail_urls),
        )
        detail_items = [
            (car_id, detail_urls[car_id])
            for car_id in ordered_car_ids
            if car_id in detail_urls and car_id not in completed_before_detail
        ]
        if max_detail_pages == 0:
            detail_items = []
        elif max_detail_pages > 0:
            detail_items = detail_items[:max_detail_pages]

        for car_id, detail_url in detail_items:
            if run_budget is not None:
                blocked = run_budget.try_consume(detail_pages=1)
                if blocked is not None:
                    print(
                        f"  Detail enrichment paused before lot {car_id}: "
                        f"{blocked.value}."
                    )
                    break

            self.database.mark_detail_attempted(
                car_id,
                listing.exleasingcar_auction_id,
                attempted_at=datetime.now(timezone.utc),
            )
            details_attempted += 1
            attempted_detail_ids.add(car_id)
            detail_response_start = len(self.recorder.responses)
            qualification = self._enrich_vehicle_detail(
                auction,
                car_id,
                detail_url,
                page,
                observed_at,
                auction_dir,
            )
            # A slow detail-page API request must not be aborted by the next
            # vehicle navigation. Give it one extended bounded wait, then stop
            # this detail rotation if it is still pending.
            if self.recorder.pending_requests:
                self.recorder.wait_for_pending(page, timeout_ms=10_000)
            detail_api_responses = self.recorder.responses[detail_response_start:]
            if detail_api_responses:
                page_id = lot_page_ids.get(car_id)
                if page_id is None:
                    raise RuntimeError(
                        "Cannot persist API responses from detail page "
                        f"{car_id}: no catalogue checkpoint owns this lot"
                    )
                self.database.append_catalog_api_responses(
                    page_id,
                    tuple(
                        {
                            "url": captured.url,
                            "payload": captured.payload,
                            "observed_at": captured.observed_at,
                        }
                        for captured in detail_api_responses
                    ),
                )
            if qualification is None:
                failed_detail_ids.append(car_id)
            else:
                details_processed += 1
                if qualification == "open_auction":
                    eligible_car_ids.add(car_id)
                    open_auction_car_ids.add(car_id)
                elif qualification == "fixed_price":
                    eligible_car_ids.add(car_id)
                    open_auction_car_ids.discard(car_id)
                else:
                    eligible_car_ids.discard(car_id)
                    open_auction_car_ids.discard(car_id)
            if self.recorder.pending_requests:
                print(
                    "  Detail rotation paused because an API response is still "
                    "pending; no next vehicle navigation will be started."
                )
                break
            if self.page_delay_seconds:
                time.sleep(self.page_delay_seconds)

        # Detail-page API responses are appended to the durable queue during
        # rotation. Replay them here; already completed lots remain filtered.
        self._qualify_open_candidates_from_catalog(
            auction,
            catalog_lot_observations,
            open_auction_candidate_ids,
            eligible_car_ids,
            open_auction_car_ids,
        )
        completed_before_api = self.database.get_completed_open_auction_lots(
            listing.exleasingcar_auction_id,
            eligible_car_ids,
        )
        self._persist_catalog_lot_observations(
            auction,
            catalog_lot_observations,
            open_auction_car_ids,
            completed_before_run=completed_before_api,
        )
        self._persist_api_updates(
            auction,
            eligible_car_ids,
            all_lot_metadata,
            open_auction_car_ids=open_auction_car_ids,
            open_auction_candidate_ids=open_auction_candidate_ids,
            run_budget=run_budget,
        )

        coverage = _catalog_detail_coverage(
            lots_seen,
            set(all_lot_metadata),
            set(detail_urls),
        )
        unattempted_detail_ids = sorted(set(detail_urls) - attempted_detail_ids)

        print(
            f"Auction {listing.exleasingcar_auction_id}: "
            f"{pages_processed} page(s) processed this run, "
            f"{lots_seen} persisted lot card(s), "
            f"{details_processed} detail page(s)"
        )
        if progress is not None and progress.status != "completed":
            print(
                f"  Catalogue scan remains resumable at "
                f"page {progress.next_page_number}: {progress.next_page_url}"
            )
        print(
            f"  Catalogue IDs: {coverage.unique_lot_ids} unique, "
            f"{coverage.repeated_card_occurrences} repeated card appearance(s)"
        )
        print(
            f"  Detail links: {coverage.detail_urls_found} found, "
            f"{details_attempted} attempted, {details_processed} processed, "
            f"{len(failed_detail_ids)} failed"
        )
        if coverage.lot_ids_without_detail_url:
            print(
                "  Lots without a recognized detail URL: "
                + ", ".join(coverage.lot_ids_without_detail_url)
            )
        if failed_detail_ids:
            print("  Detail pages not processed: " + ", ".join(failed_detail_ids))
        if unattempted_detail_ids:
            print(
                "  Detail pages deferred by weekly refresh, per-auction limit, "
                "or run budget: "
                + ", ".join(unattempted_detail_ids)
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
        run_limits: RunLimits | None = None,
        bootstrap: bool = False,
        rediscover_auctions: bool = False,
        discover_only: bool = False,
    ) -> CollectorSummary:
        """Run a bounded manual scan or a resumable all-auction bootstrap."""
        if max_auctions < 0:
            raise ValueError("max_auctions must be zero (all) or a positive integer")
        if max_detail_pages < -1:
            raise ValueError("max_detail_pages must be -1 (all), 0 (none), or positive")
        if not isinstance(bootstrap, bool):
            raise TypeError("bootstrap must be bool")
        if not isinstance(rediscover_auctions, bool):
            raise TypeError("rediscover_auctions must be bool")
        if not isinstance(discover_only, bool):
            raise TypeError("discover_only must be bool")
        if rediscover_auctions and not bootstrap:
            raise ValueError("rediscover_auctions requires bootstrap=True")
        if discover_only and not bootstrap:
            raise ValueError("discover_only requires bootstrap=True")

        if run_limits is None:
            run_limits = RunLimits(
                max_duration_seconds=900,
                max_catalog_pages=40,
                max_live_lots=500,
                max_detail_pages=60,
            )
        if not isinstance(run_limits, RunLimits):
            raise TypeError("run_limits must be a RunLimits instance")
        run_budget = RunBudget(run_limits)

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SystemExit(
                "Playwright is not installed. Run: "
                "python -m pip install -r requirements-collector.txt"
            ) from exc

        self.database.initialize()
        run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        snapshot_dir = self.raw_dir / run_stamp
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        profile_dir.mkdir(parents=True, exist_ok=True)

        pages_processed = 0
        auction_list_pages_processed = 0
        lots_seen = 0
        details_processed = 0
        auctions_discovered = 0
        auctions_processed = 0
        selected_auctions: list[ParsedAuctionListing] = []
        queue_summary = self.database.get_bootstrap_queue_summary()

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=False,
                viewport={"width": 1440, "height": 1000},
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                self.recorder.attach(context)
                print("Opening Exleasingcar in the collector's own Chromium profile...")
                page.goto(auction_list_url, wait_until="domcontentloaded", timeout=60_000)
                _wait_for_dynamic_content(page)
                if not self.recorder.wait_for_pending(page):
                    raise RuntimeError(
                        "Auction-list page still has pending source requests; "
                        "the run stopped without starting discovery."
                    )
                auctions_html = page.content()
                auctions = parse_auction_listings(auctions_html, page.url)

                # Public auction lists can remain visible after session expiry.
                # Authenticate before collecting protected vehicle pages.
                if not self._is_logged_in(page):
                    self._authenticate(
                        page,
                        reason="no authenticated account header was found at startup",
                    )
                    page.goto(
                        auction_list_url,
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                    _wait_for_dynamic_content(page)
                    if not self.recorder.wait_for_pending(page):
                        raise RuntimeError(
                            "Auction-list page still has pending requests after login."
                        )
                    auctions_html = page.content()
                    auctions = parse_auction_listings(auctions_html, page.url)
                else:
                    self._click_continue_if_visible(page)

                self._save_html(snapshot_dir / "auction_list.html", auctions_html)

                if bootstrap:
                    discovery, auction_list_pages_processed = (
                        self._discover_auction_pages(
                            page,
                            initial_page_url=page.url,
                            initial_html=auctions_html,
                            snapshot_dir=snapshot_dir,
                            run_budget=run_budget,
                            force=rediscover_auctions,
                        )
                    )
                    # Ignore auction-list API traffic; it is not scoped to one
                    # vehicle auction and must not enter the lot API replay queue.
                    self.api_cursor = len(self.recorder.responses)
                    queue_summary = self.database.get_bootstrap_queue_summary()
                    auctions_discovered = queue_summary.total
                    if discovery.status != "completed":
                        print(
                            "Global auction discovery is not complete yet; "
                            "no auction bootstrap work was started."
                        )
                    elif discover_only:
                        print("Auction discovery completed; bootstrap queue is ready.")
                    else:
                        queue_items = self.database.get_bootstrap_queue(
                            limit=max_auctions,
                            exleasingcar_auction_id=auction_id,
                        )
                        selected_auctions = [
                            self._bootstrap_listing(item) for item in queue_items
                        ]
                        print(
                            "Bootstrap queue: "
                            f"{queue_summary.completed} completed, "
                            f"{queue_summary.in_progress} in progress, "
                            f"{queue_summary.pending} pending, "
                            f"{queue_summary.deferred} deferred, "
                            f"{queue_summary.blocked} blocked (no source URL)."
                        )
                        for item, listing in zip(queue_items, selected_auctions):
                            budget_state = run_budget.snapshot()
                            if (
                                budget_state.expired
                                or budget_state.catalog_pages_used
                                >= run_limits.max_catalog_pages
                            ):
                                print(
                                    "  Bootstrap batch stopped before the next auction: "
                                    "catalogue/time budget exhausted."
                                )
                                break

                            self.database.mark_bootstrap_auction_started(
                                listing.exleasingcar_auction_id
                            )
                            try:
                                page_count, lot_count, detail_count = self._collect_auction(
                                    listing,
                                    page,
                                    snapshot_dir,
                                    max_detail_pages=max_detail_pages,
                                    run_budget=run_budget,
                                )
                            except CataloguePageMismatchError as exc:
                                self.database.mark_bootstrap_auction_failed(
                                    listing.exleasingcar_auction_id,
                                    error_code="collection_error",
                                    retry_after_seconds=1_800,
                                )
                                print(
                                    f"  Deferred auction {listing.exleasingcar_auction_id}: "
                                    f"{exc}. Retry is scheduled in 30 minutes; "
                                    "continuing with the next auction."
                                )
                                continue
                            except Exception:
                                self.database.mark_bootstrap_auction_failed(
                                    listing.exleasingcar_auction_id,
                                    error_code="collection_error",
                                )
                                raise

                            auctions_processed += 1
                            pages_processed += page_count
                            lots_seen += lot_count
                            details_processed += detail_count

                            if self.database.has_completed_catalog_scan(
                                listing.exleasingcar_auction_id,
                                "bootstrap",
                            ):
                                self.database.mark_bootstrap_auction_completed(
                                    listing.exleasingcar_auction_id
                                )
                            else:
                                # Resource-budget stops and recoverable source
                                # navigation failures retain the unfinished scan.
                                self.database.mark_bootstrap_auction_paused(
                                    listing.exleasingcar_auction_id
                                )

                            budget_state = run_budget.snapshot()
                            if (
                                budget_state.expired
                                or budget_state.catalog_pages_used
                                >= run_limits.max_catalog_pages
                            ):
                                break

                    queue_summary = self.database.get_bootstrap_queue_summary()
                    print(
                        "Bootstrap queue after this run: "
                        f"{queue_summary.completed}/{queue_summary.total} completed; "
                        f"{queue_summary.pending} pending, "
                        f"{queue_summary.in_progress} resumable, "
                        f"{queue_summary.deferred} waiting for retry, "
                        f"{queue_summary.blocked} blocked (no source URL)."
                    )
                else:
                    if not auctions:
                        raise RuntimeError(
                            "No auction cards were found after login. Check the page "
                            "address and whether the account can access the auction list."
                        )
                    # Preserve the original one-page manual mode for controlled samples.
                    self.api_cursor = len(self.recorder.responses)
                    for listing in auctions:
                        self.database.upsert_auction(
                            listing.exleasingcar_auction_id,
                            source_auction_name=listing.source_auction_name,
                            source_auction_country_code=listing.source_auction_country_code,
                            source_auction_country_name=listing.source_auction_country_name,
                            auction_type_label=listing.auction_type_label,
                            auction_status_label=listing.auction_status_label,
                            source_url=listing.source_url,
                            markers=listing.markers,
                        )

                    auctions_discovered = len(auctions)
                    selected_auctions = auctions
                    if auction_id:
                        selected_auctions = [
                            item
                            for item in selected_auctions
                            if item.exleasingcar_auction_id == auction_id
                        ]
                        if not selected_auctions:
                            raise ValueError(
                                f"Auction {auction_id} is not present on the current list page"
                            )
                    if max_auctions > 0:
                        selected_auctions = selected_auctions[:max_auctions]

                    for listing in selected_auctions:
                        try:
                            page_count, lot_count, detail_count = self._collect_auction(
                                listing,
                                page,
                                snapshot_dir,
                                max_detail_pages=max_detail_pages,
                                run_budget=run_budget,
                            )
                        except CataloguePageMismatchError as exc:
                            print(
                                f"  Skipped auction {listing.exleasingcar_auction_id}: "
                                f"{exc}. Its catalogue scan remains resumable."
                            )
                            continue
                        auctions_processed += 1
                        pages_processed += page_count
                        lots_seen += lot_count
                        details_processed += detail_count

                print(f"Raw snapshots saved under: {snapshot_dir}")
            finally:
                context.close()

        return CollectorSummary(
            auctions_discovered=auctions_discovered,
            auctions_processed=auctions_processed,
            catalog_pages_processed=pages_processed,
            lots_seen=lots_seen,
            detail_pages_processed=details_processed,
            api_responses_seen=self.api_responses_seen,
            api_cars_processed=self.api_cars_processed,
            prices_checked=self.prices_checked,
            prices_inserted=self.prices_inserted,
            prices_unchanged=self.prices_unchanged,
            seller_price_references_checked=self.price_references_checked,
            seller_price_references_inserted=self.price_references_inserted,
            seller_price_references_unchanged=self.price_references_unchanged,
            events_checked=self.events_checked,
            events_inserted=self.events_inserted,
            events_unchanged=self.events_unchanged,
            sale_types_recognized=self.sale_types_recognized,
            lot_price_observations_checked=self.lot_price_observations_checked,
            lot_price_observations_inserted=self.lot_price_observations_inserted,
            lot_price_observations_unchanged=self.lot_price_observations_unchanged,
            qualified_lots=self.qualified_lots,
            lots_with_fixed_price=self.lots_with_fixed_price,
            excluded_unpriced_lots=self.excluded_unpriced_lots,
            excluded_closed_lots=self.excluded_closed_lots,
            excluded_unknown_lots=self.excluded_unknown_lots,
            auction_list_pages_processed=auction_list_pages_processed,
            bootstrap_queue_total=queue_summary.total if bootstrap else 0,
            bootstrap_queue_pending=queue_summary.pending if bootstrap else 0,
            bootstrap_queue_in_progress=queue_summary.in_progress if bootstrap else 0,
            bootstrap_queue_completed=queue_summary.completed if bootstrap else 0,
            bootstrap_queue_deferred=queue_summary.deferred if bootstrap else 0,
            bootstrap_queue_blocked=queue_summary.blocked if bootstrap else 0,
        )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Scan Exleasingcar auctions via a manually authenticated browser and "
            "store public auction data in SQLite."
        )
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument(
        "--lock-file",
        type=Path,
        help="Collector lock path (default: next to the database)",
    )
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument(
        "--save-html-snapshots",
        action="store_true",
        default=False,
        help=(
            "Save sanitized catalogue and vehicle-page HTML snapshots. "
            "Disabled by default because authenticated pages may contain "
            "account-specific information."
        ),
    )
    # Keep the old opt-out flag accepted for existing commands.
    parser.add_argument(
        "--no-html-snapshots",
        dest="save_html_snapshots",
        action="store_false",
        help=argparse.SUPPRESS,
    )
    parser.set_defaults(save_html_snapshots=False)
    parser.add_argument("--auction-list-url", default=AUCTION_LIST_URL)
    parser.add_argument("--auction-id", help="Process only this auction ID")
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Discover every auction-list page and resume the persistent global bootstrap queue",
    )
    parser.add_argument(
        "--rediscover-auctions",
        action="store_true",
        help="Force a fresh auction-list discovery generation (requires --bootstrap)",
    )
    parser.add_argument(
        "--discover-only",
        action="store_true",
        help="Only complete auction-list discovery and queue creation (requires --bootstrap)",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Print the persisted bootstrap queue status without opening Chromium",
    )
    parser.add_argument(
        "--max-auctions",
        type=int,
        default=1,
        help="Maximum auctions per run; 0 means all due bootstrap-queue entries (default: 1)",
    )
    parser.add_argument(
        "--max-detail-pages",
        type=int,
        default=30,
        help="Detail pages per auction: -1 all, 0 none, positive number (default: 30)",
    )
    parser.add_argument(
        "--page-delay",
        type=float,
        default=1.5,
        help="Seconds to pause between page navigations (default: 1.5)",
    )
    parser.add_argument(
        "--max-run-seconds",
        type=float,
        default=900,
        help="Maximum wall-clock duration for the entire invocation (default: 900)",
    )
    parser.add_argument(
        "--no-time-limit",
        action="store_true",
        help=(
            "Disable only the wall-clock deadline. Catalogue-page, live-lot, "
            "and detail-page budgets remain enforced."
        ),
    )
    parser.add_argument(
        "--max-catalog-pages-per-run",
        type=int,
        default=40,
        help="Maximum catalogue page navigations across all auctions (default: 40)",
    )
    parser.add_argument(
        "--max-live-lots-per-run",
        type=int,
        default=500,
        help="Maximum open-lot API updates processed across all auctions (default: 500)",
    )
    parser.add_argument(
        "--max-detail-pages-per-run",
        type=int,
        default=60,
        help="Maximum detail pages across all auctions (default: 60)",
    )
    return parser


def main() -> None:
    args = build_argument_parser().parse_args()
    if args.max_auctions < 0:
        raise SystemExit("--max-auctions must be zero (all) or a positive integer")
    if args.max_detail_pages < -1:
        raise SystemExit("--max-detail-pages must be -1, 0 or a positive integer")
    if args.rediscover_auctions and not args.bootstrap:
        raise SystemExit("--rediscover-auctions requires --bootstrap")
    if args.discover_only and not args.bootstrap:
        raise SystemExit("--discover-only requires --bootstrap")
    if args.plan_only and (args.bootstrap or args.discover_only or args.rediscover_auctions):
        raise SystemExit("--plan-only cannot be combined with bootstrap execution flags")
    if args.page_delay < 0:
        raise SystemExit("--page-delay cannot be negative")
    try:
        run_limits = RunLimits(
            max_duration_seconds=(
                None if args.no_time_limit else args.max_run_seconds
            ),
            max_catalog_pages=args.max_catalog_pages_per_run,
            max_live_lots=args.max_live_lots_per_run,
            max_detail_pages=args.max_detail_pages_per_run,
        )
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"Invalid per-run collection limit: {exc}") from exc

    lock_file = args.lock_file or DEFAULT_DATABASE.parent / "collector.lock"
    try:
        with CollectorProcessLock(lock_file):
            if args.plan_only:
                if not args.database.is_file():
                    raise SystemExit(
                        f"Database not found: {args.database}. Run --bootstrap first."
                    )
                database = ArchiveDatabase(args.database)
                database.initialize()
                discovery = database.get_latest_auction_discovery_progress()
                queue = database.get_bootstrap_queue_summary()
                print("Auction discovery:")
                if discovery is None:
                    print("  not started")
                else:
                    print(
                        f"  {discovery.status}; {discovery.pages_completed} page(s), "
                        f"{discovery.auction_cards_seen} auction card appearance(s)"
                    )
                print("Bootstrap queue:")
                print(f"  total: {queue.total}")
                print(f"  completed: {queue.completed}")
                print(f"  in_progress: {queue.in_progress}")
                print(f"  pending: {queue.pending}")
                print(f"  deferred: {queue.deferred}")
                print(f"  blocked (no source URL): {queue.blocked}")
                print("No browser was opened and no collection work was performed.")
                return

            print("Effective run limits:")
            duration_label = (
                "unlimited"
                if run_limits.max_duration_seconds is None
                else str(run_limits.max_duration_seconds)
            )
            print(f"  max_duration_seconds: {duration_label}")
            print(f"  max_catalog_pages: {run_limits.max_catalog_pages}")
            print(f"  max_live_lots: {run_limits.max_live_lots}")
            print(f"  max_detail_pages: {run_limits.max_detail_pages}")
            print(f"  process_lock: {lock_file}")

            collector = ExleasingcarCollector(
                ArchiveDatabase(args.database),
                args.raw_dir,
                page_delay_seconds=args.page_delay,
                save_html_snapshots=args.save_html_snapshots,
            )
            summary = collector.run(
                auction_list_url=args.auction_list_url,
                profile_dir=args.profile_dir,
                max_auctions=args.max_auctions,
                auction_id=args.auction_id,
                max_detail_pages=args.max_detail_pages,
                run_limits=run_limits,
                bootstrap=args.bootstrap,
                rediscover_auctions=args.rediscover_auctions,
                discover_only=args.discover_only,
            )
            print("\nCollection completed")
            for field, value in asdict(summary).items():
                print(f"  {field}: {value}")
            if collector.recorder.errors:
                print("API response capture warnings:")
                for message in collector.recorder.errors[:20]:
                    print(f"  {message}")
    except CollectorAlreadyRunning as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
