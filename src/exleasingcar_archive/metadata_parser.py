"""Extract auction, catalogue-lot, and vehicle metadata from Exleasingcar HTML."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

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


@dataclass(frozen=True, slots=True)
class ParsedAuctionLotMetadata:
    """Identifiers visible on one vehicle card inside an auction catalogue."""

    source_car_id: str
    displayed_lot_reference: str | None
    listing_code_attribute: str | None


@dataclass(frozen=True, slots=True)
class ParsedAuctionCatalogMetadata:
    """Auction identifier and lot metadata from an auction catalogue page."""

    exleasingcar_auction_id: str | None
    lots: tuple[ParsedAuctionLotMetadata, ...]


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
    if page_url:
        match = re.search(r"/auto-details/(\d+)", page_url)
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
    match = AUCTION_ID_PATTERN.search(url)
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


def _parse_auction_card(
    card: Tag,
    page_url: str | None,
) -> ParsedAuctionListing | None:
    link = card.find_parent("a", href=AUCTION_ID_PATTERN) or card.find(
        "a", href=AUCTION_ID_PATTERN
    )
    href = str(link.get("href")) if isinstance(link, Tag) else None
    source_url = urljoin(page_url or "https://www.exleasingcar.com/", href) if href else None

    auction_id = _auction_id_from_url(href)
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

    type_image = card.find(
        "img",
        attrs={"alt": re.compile(r"аукцион", re.IGNORECASE)},
    )
    auction_type_label = None
    if isinstance(type_image, Tag):
        auction_type_label = (
            str(type_image.get("alt") or type_image.get("title") or "").strip()
            or None
        )

    auction_status_label = None
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
    auction_id = _auction_id_from_catalog(soup, page_url)
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
                displayed_lot_reference=displayed_reference,
                listing_code_attribute=listing_code,
            )
        )

    return ParsedAuctionCatalogMetadata(
        exleasingcar_auction_id=auction_id,
        lots=tuple(lots),
    )


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

    return ParsedVehicleMetadata(
        source_car_id=car_id,
        exleasingcar_auction_id=auction_id,
        title=title,
        location_text=location_text,
        location_country_code=location_country_code,
        location_country_name=location_country_name,
        last_registration_country_code=registration_country_code,
        last_registration_country_name=registration_country_name,
    )
