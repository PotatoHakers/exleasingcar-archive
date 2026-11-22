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
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from .api_parser import parse_car_update_response
from .database import ArchiveDatabase
from .detail_parser import parse_vehicle_detail
from .ingestion import AuctionContext, LotMetadata, store_car_updates
from .metadata_parser import (
    ParsedAuctionListing,
    ParsedAuctionLotMetadata,
    parse_auction_catalog_metadata,
    parse_auction_listings,
    parse_catalog_pagination,
    parse_vehicle_media,
    parse_vehicle_metadata,
)
from .models import SaleType

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
    excluded_closed_lots: int
    excluded_unknown_lots: int


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
        self._timed_out_request_ids: set[int] = set()
        self._responses_received: set[int] = set()

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

    def _capture_response(self, response: Any) -> bool:
        if API_ENDPOINT_MARKER not in response.url:
            return False
        if not response.ok:
            self.errors.append(f"HTTP {response.status}: {response.url}")
            return False
        try:
            payload = json.loads(response.body())
        except (PlaywrightError, ValueError, TypeError, UnicodeDecodeError) as exc:
            content_type = getattr(response, "headers", {}).get("content-type", "unknown")
            self.errors.append(
                f"Could not read JSON from {response.url} "
                f"(HTTP {response.status}, content-type={content_type}): {exc}"
            )
            return False
        if not isinstance(payload, Mapping):
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
        self._responses_received.add(id(request))
        self._capture_response(response)

    def _on_request_finished(self, request: Any) -> None:
        request_id = id(request)
        if self.pending_requests.pop(request_id, None) is None:
            return
        self._timed_out_request_ids.discard(request_id)
        if request_id not in self._responses_received:
            # Keep a fallback for contexts where a response event was not
            # observed, but the completed request still exposes its response.
            try:
                response = request.response()
            except PlaywrightError as exc:
                self.errors.append(
                    f"Could not access completed API response from {request.url}: {exc}"
                )
                return
            if response is None:
                self.errors.append(
                    f"API request finished without an HTTP response: {request.url}"
                )
                return
            self._capture_response(response)
        self._responses_received.discard(request_id)

    def _on_request_failed(self, request: Any) -> None:
        request_id = id(request)
        if self.pending_requests.pop(request_id, None) is None:
            return
        self._timed_out_request_ids.discard(request_id)
        if request_id in self._responses_received:
            # The response handler already recorded either the payload or its
            # parsing/body-read error; avoid a second misleading abort warning.
            self._responses_received.discard(request_id)
            return
        failure = getattr(request, "failure", None)
        if callable(failure):
            failure = failure()
        self.errors.append(
            "API request failed before its response body could be captured: "
            f"{request.url} ({failure or 'unknown network failure'})"
        )

    def wait_for_pending(self, page: Any, *, timeout_ms: int = 3_000) -> bool:
        """Wait briefly for target API bodies before the next page navigation."""
        if timeout_ms < 0:
            raise ValueError("timeout_ms cannot be negative")
        deadline = time.monotonic() + timeout_ms / 1000
        while self.pending_requests and time.monotonic() < deadline:
            page.wait_for_timeout(50)

        pending_ids = set(self.pending_requests)
        newly_timed_out = pending_ids - self._timed_out_request_ids
        if newly_timed_out:
            urls = sorted(
                {
                    str(self.pending_requests[request_id].url)
                    for request_id in newly_timed_out
                }
            )
            self.errors.append(
                f"Timed out waiting for {len(newly_timed_out)} API request(s) "
                f"before navigation: {'; '.join(urls)}"
            )
            self._timed_out_request_ids.update(newly_timed_out)
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
    ) -> None:
        """Persist qualified offers until each open auction reaches a final state.

        The final terminal response remains in history; later responses for that
        lot are ignored. Fixed-price-only lots retain seller references, but
        hidden-bid and auction-state fields are discarded.
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
        completed_during_run: set[str] = set()

        while self.api_cursor < len(self.recorder.responses):
            captured = self.recorder.responses[self.api_cursor]
            self.api_cursor += 1
            self.api_responses_seen += 1
            try:
                updates = parse_car_update_response(captured.payload)
            except (TypeError, ValueError) as exc:
                print(f"  Skipped malformed API response: {exc}")
                continue

            selected = filter_updates_for_catalog(updates, eligible_car_ids)
            selected = {
                car_id: update
                for car_id, update in selected.items()
                if car_id not in completed_before_run
                and car_id not in completed_during_run
            }
            if not selected:
                continue

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
                        f"  Open auction {car_id} completed; "
                        "final observation retained and further monitoring stopped."
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
            self.recorder.wait_for_pending(page)
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
                self.recorder.wait_for_pending(page)
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
                self.recorder.wait_for_pending(page)
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
        fixed_price_available = parsed_detail.fixed_price is not None
        archive_eligible = parsed_detail.should_archive
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
            else:
                self.excluded_unknown_lots += 1
                outcome = "excluded_unknown"
                reason = "unknown or unpriced sale mode"
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
            if price is None:
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
            markers=listing.markers,
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
        details_attempted = 0
        failed_detail_ids: list[str] = []
        attempted_detail_ids: set[str] = set()
        observed_at = datetime.now(timezone.utc)

        while current_url and current_url not in visited_urls:
            visited_urls.add(current_url)
            try:
                page.goto(current_url, wait_until="domcontentloaded", timeout=45_000)
                _wait_for_dynamic_content(page)
                self.recorder.wait_for_pending(page)
            except PlaywrightError as exc:
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

        # API responses remain in memory until each lot has been qualified.
        if (
            listing.vehicle_count is not None
            and listing.vehicle_count > len(all_lot_metadata)
        ):
            print(
                f"  WARNING: auction list reports {listing.vehicle_count} vehicles, "
                f"but only {len(all_lot_metadata)} unique lot IDs were found. "
                "Catalog may be incomplete."
            )

        ordered_car_ids = self.database.get_detail_check_order(
            listing.exleasingcar_auction_id,
            set(detail_urls),
        )
        detail_items = [
            (car_id, detail_urls[car_id]) for car_id in ordered_car_ids
            if car_id in detail_urls
        ]
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
        if max_detail_pages == 0:
            detail_items = []
        elif max_detail_pages > 0:
            detail_items = detail_items[:max_detail_pages]
        for car_id, detail_url in detail_items:
            self.database.mark_detail_attempted(
                car_id,
                listing.exleasingcar_auction_id,
                attempted_at=datetime.now(timezone.utc),
            )
            details_attempted += 1
            attempted_detail_ids.add(car_id)
            qualification = self._enrich_vehicle_detail(
                auction,
                car_id,
                detail_url,
                page,
                observed_at,
                auction_dir,
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
            if self.page_delay_seconds:
                time.sleep(self.page_delay_seconds)

        # Only API rows belonging to known or newly qualified lots are persisted.
        # Fixed-price-only lots keep seller references but not hidden-bid fields.
        self._persist_api_updates(
            auction,
            eligible_car_ids,
            all_lot_metadata,
            open_auction_car_ids=open_auction_car_ids,
        )

        coverage = _catalog_detail_coverage(
            lots_seen,
            set(all_lot_metadata),
            set(detail_urls),
        )
        unattempted_detail_ids = sorted(set(detail_urls) - attempted_detail_ids)

        print(
            f"Auction {listing.exleasingcar_auction_id}: "
            f"{pages_processed} page(s), {lots_seen} lot card(s), "
            f"{details_processed} detail page(s)"
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
                "  Detail pages deferred by weekly refresh or --max-detail-pages: "
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
                print("Opening Exleasingcar in the collector's own Chromium profile...")
                page.goto(auction_list_url, wait_until="domcontentloaded", timeout=60_000)
                _wait_for_dynamic_content(page)
                self.recorder.wait_for_pending(page)
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
                    self.recorder.wait_for_pending(page)
                    auctions_html = page.content()
                    auctions = parse_auction_listings(auctions_html, page.url)
                else:
                    self._click_continue_if_visible(page)

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
                        markers=listing.markers,
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
            excluded_closed_lots=self.excluded_closed_lots,
            excluded_unknown_lots=self.excluded_unknown_lots,
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
        "--max-auctions",
        type=int,
        default=1,
        help="Maximum auctions to scan; 0 means all auctions on the list page (default: 1)",
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
        save_html_snapshots=args.save_html_snapshots,
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
