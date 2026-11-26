"""Extract auction, catalogue-lot, and vehicle metadata from Exleasingcar HTML."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from .models import AuctionCardMarker

AUCTION_ID_PATTERN = re.compile(r"(?:auction|аукцион)-(\d+)", re.IGNORECASE)
FLAG_PATTERN = re.compile(r"^flag-([a-z]{2})$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ParsedAuctionListing:
    """One auction card found on Exleasingcar's auction-list page."""

    exleasingcar_auction_id: str
    source_auction_name: str | None
    source_auction_country_code: str | None
    source_auction_country_name: str | None
    auction_type_label: str | None
    auction_status_label: str | None
    vehicle_count: int | None
    source_url: str | None
    markers: tuple[AuctionCardMarker, ...] = ()

    @property
    def is_open_auction(self) -> bool:
        """Return true only for the site's explicitly labelled English auction."""
        label = (self.auction_type_label or "").casefold()
        return "английск" in label or "english auction" in label

    @property
    def is_closed_auction(self) -> bool:
        """Recognize the site's explicitly labelled closed/sealed auction format."""
        label = (self.auction_type_label or "").casefold()
        return "закрыт" in label or "closed auction" in label

    @property
    def is_fixed_price_only(self) -> bool:
        """A fixed-price option is primary only when no auction format is advertised."""
        label = (self.auction_type_label or "").casefold()
        is_fixed_price = "фиксирован" in label or "fixed price" in label
        return (
            is_fixed_price
            and not self.is_open_auction
            and not self.is_closed_auction
        )

    @property
    def has_final_call(self) -> bool:
        return (self.auction_status_label or "").strip().casefold() == "final call"

    @property
    def has_hot_bid_signal(self) -> bool:
        return (self.auction_status_label or "").strip().casefold() == "hot bid"

    @property
    def has_scheduled_end_label(self) -> bool:
        """Recognize a visible end-time label without guessing its timezone."""
        label = (self.auction_status_label or "").casefold()
        return bool(
            re.search(r"\b\d{1,2}:\d{2}\b", label)
            and any(
                marker in label
                for marker in ("сегодня", "today", "завтра", "tomorrow", "конец", "end")
            )
        )

    @property
    def supports_live_bid_monitoring(self) -> bool:
        """Only English/open auctions are candidates for public bid monitoring."""
        return self.is_open_auction

    @property
    def collection_priority(self) -> int:
        """Prioritize live open auctions without mistaking fixed/closed formats for live."""
        if self.is_open_auction:
            if self.has_final_call:
                return 0
            if self.has_hot_bid_signal:
                return 1
            if self.has_scheduled_end_label:
                return 2
            return 3
        if self.is_closed_auction:
            return 4
        if self.is_fixed_price_only:
            return 5
        return 6


@dataclass(frozen=True, slots=True)
class ParsedAuctionLotMetadata:
    """Identifiers visible on one vehicle card inside an auction catalogue."""

    source_car_id: str
    displayed_title: str | None
    displayed_lot_reference: str | None
    listing_code_attribute: str | None
    make_label: str | None = None
    mileage_km: int | None = None


@dataclass(frozen=True, slots=True)
class ParsedAuctionCatalogMetadata:
    """Auction identifier and lot metadata from an auction catalogue page."""

    exleasingcar_auction_id: str | None
    lots: tuple[ParsedAuctionLotMetadata, ...]


@dataclass(frozen=True, slots=True)
class ParsedCatalogPagination:
    """Pagination links advertised by one saved auction-catalogue page."""

    current_page_url: str | None
    current_page_number: int | None
    page_urls: tuple[str, ...]
    next_page_url: str | None


@dataclass(frozen=True, slots=True)
class ParsedMediaReference:
    """One photo or inspection report advertised by a vehicle detail page."""

    media_type: str
    source_url: str
    preview_url: str | None = None
    label: str | None = None
    source_order: int = 0


MEDIA_SOURCE_HOSTS = frozenset({"data.exleasingcar.com", "portal.macadam.eu"})


@dataclass(frozen=True, slots=True)
class ParsedVehicleMetadata:
    """Metadata explicitly displayed on an individual vehicle detail page."""

    source_car_id: str | None
    exleasingcar_auction_id: str | None
    title: str | None
    location_text: str | None
    location_country_code: str | None
    location_country_name: str | None
    last_registration_country_code: str | None
    last_registration_country_name: str | None
    chassis_number: str | None = None
    chassis_number_fragment: str | None = None
    chassis_number_masked: bool | None = None


def _text(tag: Tag | None) -> str | None:
    if tag is None:
        return None
    value = " ".join(tag.get_text(" ", strip=True).split())
    return value or None


def _get_car_id(tag: Tag) -> str | None:
    car_id = tag.get("car-id")
    if car_id is not None and str(car_id).strip():
        return str(car_id).strip()
    element_id = str(tag.get("id", ""))
    match = re.fullmatch(r"auto_(\d+)", element_id)
    return match.group(1) if match else None


def _detect_car_id(soup: BeautifulSoup, page_url: str | None) -> str | None:
    for tag in soup.find_all(True):
        car_id = tag.get("car-id")
        if car_id is not None and str(car_id).strip():
            return str(car_id).strip()
    candidate_urls = [page_url]
    canonical = soup.select_one('link[rel="canonical"][href]')
    if canonical is not None:
        candidate_urls.append(str(canonical.get("href")))
    og_url = soup.select_one('meta[property="og:url"][content]')
    if og_url is not None:
        candidate_urls.append(str(og_url.get("content")))

    for candidate_url in candidate_urls:
        if not candidate_url:
            continue
        match = re.search(r"/auto-details/(\d+)", candidate_url)
        if match:
            return match.group(1)
    for tag in soup.find_all(True):
        match = re.fullmatch(r"(?:auto_body|auto)_(\d+)", str(tag.get("id", "")))
        if match:
            return match.group(1)
    return None


def _auction_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = AUCTION_ID_PATTERN.search(unquote(url))
    return match.group(1) if match else None


def _flag_country_code(container: Tag) -> str | None:
    for element in container.select(".flag-image"):
        for class_name in element.get("class", []):
            match = FLAG_PATTERN.fullmatch(str(class_name))
            if match:
                return match.group(1).upper()
    return None


def _country_name_from_value(value: str | None) -> str | None:
    if not value:
        return None
    # For a location such as "DE-04509, Germany", keep the country part,
    # while preserving the full original value separately as location_text.
    if "," in value:
        country_name = value.rsplit(",", 1)[1].strip()
        return country_name or None
    return value.strip() or None


def _first_direct_auction_card_name(card: Tag) -> str | None:
    header = card.select_one(".ac-head-items")
    if header is None:
        return None

    header_items = header.find_all(
        class_="ac-head-item",
        recursive=False,
    )
    auction_item_index: int | None = None

    for index, item in enumerate(header_items):
        item_text = _text(item) or ""
        if re.search(r"Аукцион\s*ID\s*:", item_text, re.IGNORECASE):
            auction_item_index = index
            break

    if auction_item_index is None:
        return None

    for item in header_items[auction_item_index + 1 :]:
        if item.find("img") is not None:
            continue
        value = _text(item)
        if value:
            return value
    return None


def _parse_vehicle_count(card: Tag) -> int | None:
    header = card.select_one(".ac-head-items .ac-inner")
    value = _text(header)
    if value is None:
        return None
    match = re.search(r"\b(\d+)\b", value)
    return int(match.group(1)) if match else None



def _parse_auction_card_markers(header: Tag) -> tuple[AuctionCardMarker, ...]:
    """Keep each independent SVG marker shown in the auction-card header."""
    marker_groups = {
        "ac.svg": "auction_format",
        "mail-b.svg": "auction_format",
        "euro-sign.svg": "fixed_price_option",
        "type-2.svg": "vehicle_condition",
        "type-3.svg": "vehicle_category",
        "favorite.svg": "recommendation",
    }
    markers: dict[str, AuctionCardMarker] = {}

    for image in header.find_all("img"):
        source = str(image.get("src") or "").strip()
        icon_filename = source.rsplit("/", 1)[-1].split("?", 1)[0].lower()
        if not icon_filename.endswith(".svg") or icon_filename == "car.svg":
            continue

        label = (
            str(image.get("title") or image.get("alt") or "").strip() or None
        )
        if label is not None and label.casefold() == "icon":
            label = None

        # The recommendation star has its explanation in a hidden tooltip
        # next to the image rather than in its alt/title attributes.
        if label is None:
            parent = image.parent
            for _ in range(3):
                if not isinstance(parent, Tag):
                    break
                candidate = _text(parent)
                if candidate:
                    label = candidate
                    break
                parent = parent.parent

        marker = AuctionCardMarker(
            icon_filename=icon_filename,
            marker_group=marker_groups.get(icon_filename, "other"),
            label=label,
        )
        existing = markers.get(icon_filename)
        if existing is None or (existing.label is None and marker.label is not None):
            markers[icon_filename] = marker

    return tuple(markers.values())

def _parse_auction_card(
    card: Tag,
    page_url: str | None,
) -> ParsedAuctionListing | None:
    link_candidates: list[Tag] = []
    parent_link = card.find_parent("a", href=True)
    if isinstance(parent_link, Tag):
        link_candidates.append(parent_link)
    link_candidates.extend(card.find_all("a", href=True))

    source_url = None
    auction_id = None
    for link in link_candidates:
        candidate_url = str(link.get("href") or "").strip()
        candidate_id = _auction_id_from_url(candidate_url)
        if candidate_id:
            auction_id = candidate_id
            source_url = urljoin(
                page_url or "https://www.exleasingcar.com/",
                candidate_url,
            )
            break

    # Some page versions use a JavaScript click handler instead of a link.
    if source_url is None:
        onclick = str(card.get("onclick") or "")
        for candidate_url in re.findall(
            r"""['"]((?:https?://|/)[^'"]+)['"]""",
            onclick,
        ):
            candidate_url = unescape(candidate_url)
            candidate_id = _auction_id_from_url(candidate_url)
            if candidate_id:
                auction_id = candidate_id
                source_url = urljoin(
                    page_url or "https://www.exleasingcar.com/",
                    candidate_url,
                )
                break

    if auction_id is None:
        for item in card.select(".ac-head-item"):
            item_text = _text(item) or ""
            match = re.search(
                r"Аукцион\s*ID\s*:\s*(\d+)",
                item_text,
                re.IGNORECASE,
            )
            if match:
                auction_id = match.group(1)
                break

    if auction_id is None:
        return None

    country_container = card.select_one(".ac-list-left") or card
    country_code = _flag_country_code(country_container)
    country_name = _text(country_container.select_one(".ml-2"))

    auction_header = card.select_one(".card-header .ac-head-items") or card
    type_image = auction_header.find(
        "img",
        attrs={"alt": re.compile(r"аукцион", re.IGNORECASE)},
    )
    if type_image is None:
        # The site uses the fixed-price icon as the main type only on
        # "Buy now" cards. On mixed cards the open/closed icon takes priority.
        type_image = auction_header.find(
            "img",
            attrs={"alt": re.compile(r"фиксирован", re.IGNORECASE)},
        )

    auction_type_label = None
    if isinstance(type_image, Tag):
        auction_type_label = (
            str(type_image.get("alt") or type_image.get("title") or "").strip()
            or None
        )
    auction_markers = _parse_auction_card_markers(auction_header)

    auction_status_label = None
    status_area = card.select_one(".ac-list-left")
    if isinstance(status_area, Tag):
        for candidate in status_area.select(
            ".ac-head-item, .ac-head-inner, button"
        ):
            candidate_text = _text(candidate)
            if not candidate_text:
                continue
            normalized = candidate_text.casefold()
            is_status_label = (
                any(
                    marker in normalized
                    for marker in (
                        "final call",
                        "hot bid",
                        "сегодня",
                        "today",
                        "завтра",
                        "tomorrow",
                        "конец",
                        "end",
                    )
                )
                or re.search(r"\b\d{1,2}:\d{2}\b", candidate_text) is not None
            )
            if is_status_label:
                auction_status_label = candidate_text
                break

    # Keep the previous fallback for older page variants where the clock/status
    # area is not wrapped in the current .ac-list-left structure.
    if auction_status_label is None:
        for button in card.find_all("button"):
            button_text = _text(button)
            if button_text:
                auction_status_label = button_text
                break

    return ParsedAuctionListing(
        exleasingcar_auction_id=auction_id,
        source_auction_name=_first_direct_auction_card_name(card),
        source_auction_country_code=country_code,
        source_auction_country_name=country_name,
        auction_type_label=auction_type_label,
        auction_status_label=auction_status_label,
        vehicle_count=_parse_vehicle_count(card),
        source_url=source_url,
        markers=auction_markers,
    )


def parse_auction_listings(
    html: str,
    page_url: str | None = None,
) -> tuple[ParsedAuctionListing, ...]:
    """Parse auction cards from the main Exleasingcar auction-list page."""
    soup = BeautifulSoup(html, "html.parser")
    parsed: list[ParsedAuctionListing] = []
    seen_ids: set[str] = set()

    for card in soup.select(".ac-list-item"):
        auction = _parse_auction_card(card, page_url)
        if auction is None or auction.exleasingcar_auction_id in seen_ids:
            continue
        parsed.append(auction)
        seen_ids.add(auction.exleasingcar_auction_id)

    return tuple(parsed)


def _auction_id_from_catalog(soup: BeautifulSoup, page_url: str | None) -> str | None:
    auction_input = soup.find("input", attrs={"name": "aukciono_id"})
    if isinstance(auction_input, Tag):
        value = auction_input.get("value")
        if value is not None and str(value).strip():
            return str(value).strip()
    return _auction_id_from_url(page_url)


def _catalog_card_candidates(soup: BeautifulSoup) -> Iterable[Tag]:
    seen_ids: set[str] = set()
    for candidate in soup.select("[car-id], [id^='auto_']"):
        car_id = _get_car_id(candidate)
        if car_id is None or car_id in seen_ids:
            continue
        seen_ids.add(car_id)
        yield candidate


def _parse_mileage_km(card: Tag) -> int | None:
    """Read an unmasked kilometre reading from a catalogue card."""
    for element in card.select(".mb-2.px-2"):
        value = _text(element) or ""
        match = re.search(r"(\d[\d\s.,]*)\s*km\b", value, re.IGNORECASE)
        if match is None:
            continue
        digits = re.sub(r"\D", "", match.group(1))
        if digits:
            return int(digits)
    return None


def _make_label(card: Tag) -> str | None:
    """Keep only the leading make confirmed by both the image alt and title.

    The catalogue's image alt can contain a stale model name from another
    vehicle card. The leading words shared with this card's displayed title
    are the only portion treated as a make; a disagreement stays unknown.
    """
    image = card.select_one("img.card-img-top[alt]")
    title = _text(card.select_one("h5, .car-title"))
    if image is None or title is None:
        return None

    image_alt = str(image.get("alt") or "").strip()
    image_tokens = re.findall(
        r"[^\W_]+(?:[-'][^\W_]+)*",
        image_alt,
        flags=re.UNICODE,
    )
    title_tokens = re.findall(
        r"[^\W_]+(?:[-'][^\W_]+)*",
        title,
        flags=re.UNICODE,
    )

    confirmed_tokens = 0
    for image_token, title_token in zip(image_tokens, title_tokens):
        if image_token.casefold() != title_token.casefold():
            break
        confirmed_tokens += 1

    if confirmed_tokens == 0:
        return None
    return " ".join(image_tokens[:confirmed_tokens])


def _parse_chassis_number(
    value_cell: Tag | None,
) -> tuple[str | None, str | None, bool | None]:
    """Return a complete chassis number or a visible fragment when masked."""
    value = _text(value_cell)
    if not value:
        return None, None, None

    is_masked = bool(
        re.search(r"[*•…]|\.{2,}|\bhidden\b", value, re.IGNORECASE)
    )
    visible_parts = re.findall(r"[A-Z0-9]+", value.upper())
    fragment = "".join(visible_parts) or None
    if is_masked:
        return None, fragment, True

    normalized = re.sub(r"[^A-Z0-9]", "", value.upper())
    if not normalized:
        return None, None, False
    return normalized, None, False


def parse_auction_catalog_metadata(
    html: str,
    page_url: str | None = None,
) -> ParsedAuctionCatalogMetadata:
    """Extract auction ID and visible lot identifiers from a catalogue page.

    The first ``.mb-2.px-2`` value is preserved as a displayed lot reference.
    Its exact business meaning is not inferred. The HTML ``code`` attribute is
    stored separately as an opaque listing identifier.
    """
    soup = BeautifulSoup(html, "html.parser")
    resolved_page_url = page_url or _saved_page_url(html)
    auction_id = _auction_id_from_catalog(soup, resolved_page_url)
    lots: list[ParsedAuctionLotMetadata] = []

    for card in _catalog_card_candidates(soup):
        car_id = _get_car_id(card)
        if car_id is None:
            continue

        reference_tag = card.select_one(".mb-2.px-2")
        displayed_reference = _text(reference_tag)
        raw_code = card.get("code")
        listing_code = str(raw_code).strip() if raw_code is not None else None
        if listing_code == "":
            listing_code = None

        lots.append(
            ParsedAuctionLotMetadata(
                source_car_id=car_id,
                displayed_title=_text(card.select_one("h5, .car-title")),
                displayed_lot_reference=displayed_reference,
                listing_code_attribute=listing_code,
                make_label=_make_label(card),
                mileage_km=_parse_mileage_km(card),
            )
        )

    return ParsedAuctionCatalogMetadata(
        exleasingcar_auction_id=auction_id,
        lots=tuple(lots),
    )


def _saved_page_url(html: str) -> str | None:
    """Read the source URL embedded by browsers in saved HTML comments."""
    match = re.search(
        r"saved from url=\(\d+\)(.*?)-->",
        html[:8192],
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        return None
    value = unescape(match.group(1).strip())
    return value or None


def parse_catalog_pagination(
    html: str,
    page_url: str | None = None,
) -> ParsedCatalogPagination:
    """Discover catalogue page links without assuming a fixed page count.

    Exleasingcar currently exposes numeric ``pagination-page`` anchors and a
    separate ``pagination-next`` arrow. The arrow can repeat the URL of the
    next numbered page, so links are de-duplicated. The active numeric link is
    used as the current page URL; this is more reliable than the browser's
    saved-from comment, which may omit the ``show-20`` route segment.
    """
    soup = BeautifulSoup(html, "html.parser")
    base_url = page_url or _saved_page_url(html) or "https://www.exleasingcar.com/"
    pagination = soup.select_one("nav ul.pagination")
    if pagination is None:
        return ParsedCatalogPagination(
            current_page_url=page_url or _saved_page_url(html),
            current_page_number=None,
            page_urls=(),
            next_page_url=None,
        )

    page_urls: list[str] = []
    numbered_links: list[tuple[int, str]] = []
    current_page_url: str | None = None
    current_page_number: int | None = None

    for anchor in pagination.select("a[href]"):
        href = str(anchor.get("href", "")).strip()
        if not href:
            continue
        absolute_url = urljoin(base_url, href)
        classes = {str(value) for value in anchor.get("class", [])}
        label = _text(anchor) or ""
        number_match = re.fullmatch(r"\d+", label)
        if "pagination-page" in classes and number_match:
            page_number = int(label)
            numbered_links.append((page_number, absolute_url))
            if absolute_url not in page_urls:
                page_urls.append(absolute_url)
            if "current" in classes:
                current_page_url = absolute_url
                current_page_number = page_number
        elif "pagination-next" in classes and absolute_url not in page_urls:
            page_urls.append(absolute_url)

    if current_page_url is None and page_url:
        current_page_url = page_url
    if current_page_number is None and current_page_url:
        tail_match = re.search(r"/(\d+)/?$", current_page_url)
        if tail_match:
            current_page_number = int(tail_match.group(1))

    next_page_url: str | None = None
    next_anchor = pagination.select_one("a.pagination-next[href]")
    if isinstance(next_anchor, Tag):
        candidate = urljoin(base_url, str(next_anchor.get("href")))
        if candidate != current_page_url:
            next_page_url = candidate

    if next_page_url is None and current_page_number is not None:
        future_links = [
            (number, url)
            for number, url in numbered_links
            if number > current_page_number
        ]
        if future_links:
            next_page_url = min(future_links, key=lambda item: item[0])[1]

    return ParsedCatalogPagination(
        current_page_url=current_page_url,
        current_page_number=current_page_number,
        page_urls=tuple(page_urls),
        next_page_url=next_page_url,
    )


def parse_auction_list_pagination(
    html: str,
    page_url: str | None = None,
) -> ParsedCatalogPagination:
    """Parse the same numbered/next pagination controls on the global auction list."""
    return parse_catalog_pagination(html, page_url)


def _find_labeled_value(
    soup: BeautifulSoup,
    labels: set[str],
) -> Tag | None:
    normalized_labels = {label.casefold().rstrip(":") for label in labels}
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"], recursive=False)
        if len(cells) < 2:
            continue
        label = _text(cells[0])
        if label and label.casefold().rstrip(":") in normalized_labels:
            return cells[1]
    return None


def _extract_auction_id_from_detail(soup: BeautifulSoup) -> str | None:
    for element in soup.find_all(string=re.compile(r"ID\s*аукциона", re.IGNORECASE)):
        parent = element.parent
        if not isinstance(parent, Tag):
            continue
        anchor = parent.find("a", href=AUCTION_ID_PATTERN)
        if anchor is None and isinstance(parent.parent, Tag):
            anchor = parent.parent.find("a", href=AUCTION_ID_PATTERN)
        if isinstance(anchor, Tag):
            href = anchor.get("href")
            auction_id = _auction_id_from_url(str(href) if href else None)
            if auction_id:
                return auction_id
            anchor_text = _text(anchor)
            if anchor_text and anchor_text.isdigit():
                return anchor_text
    return None


def _trusted_media_url(value: str | None, page_url: str | None) -> str | None:
    if not value:
        return None
    candidate = urljoin(page_url or "https://www.exleasingcar.com/", value.strip())
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").casefold()
    if (
        parts.scheme.casefold() != "https"
        or host not in MEDIA_SOURCE_HOSTS
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or not parts.path
        or parts.fragment
    ):
        return None
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
            return None
    return candidate


def _onclick_url(value: str | None, function_name: str) -> str | None:
    if not value:
        return None
    pattern = re.compile(
        rf"""{re.escape(function_name)}\s*\([^'"]*(['"])(.*?)\1""",
        flags=re.IGNORECASE,
    )
    match = pattern.search(value)
    return match.group(2).strip() if match else None


def parse_vehicle_media(
    html: str,
    page_url: str | None = None,
) -> tuple[ParsedMediaReference, ...]:
    """Discover lot-specific gallery originals and inspection-report URLs.

    Only HTTPS URLs on the Exleasingcar site/media hosts are accepted. Local
    resources from browser-saved HTML (for example, *_files/image.jpg) are
    deliberately ignored because they are not stable source URLs.
    """
    soup = BeautifulSoup(html, "html.parser")
    references: list[ParsedMediaReference] = []
    seen: set[tuple[str, str]] = set()

    def add_reference(
        media_type: str,
        raw_url: str | None,
        *,
        preview_raw_url: str | None = None,
        label: str | None = None,
    ) -> None:
        source_url = _trusted_media_url(raw_url, page_url)
        if source_url is None:
            return
        key = (media_type, source_url)
        if key in seen:
            return
        seen.add(key)
        references.append(
            ParsedMediaReference(
                media_type=media_type,
                source_url=source_url,
                preview_url=_trusted_media_url(preview_raw_url, page_url),
                label=label,
                source_order=len(references),
            )
        )

    # Current Exleasingcar detail pages expose each full-size original through
    # change_thumb(this, 'https://data.exleasingcar.com/...-normal_*.jpg').
    for anchor in soup.select("#gallery-area .gallery-thumbs a[onclick]"):
        onclick = str(anchor.get("onclick") or "")
        raw_url = _onclick_url(onclick, "change_thumb")
        if raw_url is None:
            title = anchor.get("title")
            raw_url = str(title) if title else None
        image = anchor.find("img")
        preview_url = (
            str(image.get("src"))
            if isinstance(image, Tag) and image.get("src")
            else None
        )
        add_reference("photo", raw_url, preview_raw_url=preview_url)

    # Fallback for page variants that put full-size URLs directly in the
    # Highslide gallery. Relative browser-save paths are intentionally skipped.
    for anchor in soup.select("#gallery-area .highslide-gallery a.highslide[href]"):
        image = anchor.find("img")
        preview_url = (
            str(image.get("src"))
            if isinstance(image, Tag) and image.get("src")
            else None
        )
        add_reference("photo", str(anchor.get("href")), preview_raw_url=preview_url)

    # Inspection reports are opened by inline window.open(...) handlers inside
    # the vehicle's damage/inspection block. The same URL can occur twice in a
    # row, so URL-based de-duplication is required.
    for element in soup.select(".damage-block [onclick]"):
        onclick = str(element.get("onclick") or "")
        raw_url = _onclick_url(onclick, "window.open")
        if raw_url is None:
            continue
        label_tag = element.find("strong") if isinstance(element, Tag) else None
        label = _text(label_tag) or _text(element)
        add_reference("report", raw_url, label=label)

    return tuple(references)


def parse_vehicle_metadata(
    html: str,
    page_url: str | None = None,
) -> ParsedVehicleMetadata:
    """Parse metadata explicitly displayed on one vehicle detail page.

    Make and model are deliberately not split from the page heading: the
    supplied HTML shows a combined title, but does not expose a confirmed
    separate make/model pair.
    """
    soup = BeautifulSoup(html, "html.parser")
    car_id = _detect_car_id(soup, page_url)
    auction_id = _extract_auction_id_from_detail(soup)
    title = _text(soup.find("h1"))

    location_cell = _find_labeled_value(soup, {"Место расположения"})
    location_text = _text(location_cell)
    location_country_code = (
        _flag_country_code(location_cell) if location_cell is not None else None
    )
    location_country_name = (
        _country_name_from_value(location_text)
        if location_country_code is not None
        else None
    )

    registration_cell = _find_labeled_value(
        soup,
        {"Страна последней регистрации"},
    )
    registration_text = _text(registration_cell)
    registration_country_code = (
        _flag_country_code(registration_cell)
        if registration_cell is not None
        else None
    )
    registration_country_name = (
        _country_name_from_value(registration_text)
        if registration_country_code is not None
        else None
    )

    chassis_cell = _find_labeled_value(soup, {"Номер кузова", "Номер шасси"})
    chassis_number, chassis_fragment, chassis_masked = _parse_chassis_number(
        chassis_cell
    )

    return ParsedVehicleMetadata(
        source_car_id=car_id,
        exleasingcar_auction_id=auction_id,
        title=title,
        location_text=location_text,
        location_country_code=location_country_code,
        location_country_name=location_country_name,
        last_registration_country_code=registration_country_code,
        last_registration_country_name=registration_country_name,
        chassis_number=chassis_number,
        chassis_number_fragment=chassis_fragment,
        chassis_number_masked=chassis_masked,
    )
