
"""Classify auction bidding modes and extract public auction prices."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag

from .models import PriceType, SaleType
from .parsers import PriceObservation, clean_text, parse_amount


@dataclass(frozen=True, slots=True)
class ParsedVehicleDetail:
    """Auction mode and publicly displayed auction price."""

    source_car_id: str | None
    sale_type: SaleType | None
    bidding_mode_classes: tuple[str, ...]
    displayed_auction_price: PriceObservation | None
    fixed_price: PriceObservation | None
    is_hot_bid: bool

    @property
    def should_monitor(self) -> bool:
        """Only public open auctions belong to the main monitor."""

        return self.sale_type is SaleType.OPEN_AUCTION


def detect_car_id(
    soup: BeautifulSoup,
    page_url: str | None = None,
) -> str | None:
    """Find the vehicle ID from the page or URL."""

    for tag in soup.find_all(True):
        car_id = tag.get("car-id")

        if car_id:
            return str(car_id).strip()

    if page_url:
        match = re.search(r"/auto-details/(\d+)", page_url)

        if match:
            return match.group(1)

    for tag in soup.find_all(True):
        element_id = str(tag.get("id", ""))

        match = re.fullmatch(
            r"(?:auto_body|auto)_(\d+)",
            element_id,
        )

        if match:
            return match.group(1)

    return None


def find_bids_container(
    soup: BeautifulSoup,
    car_id: str | None,
) -> Tag | None:
    """Find the bidding block belonging to this vehicle."""

    candidates: list[Tag] = []

    for tag in soup.find_all("div", class_=True):
        classes = list(tag.get("class", []))

        if "bids" not in classes:
            continue

        if car_id and f"bids_{car_id}" in classes:
            return tag

        candidates.append(tag)

    if len(candidates) == 1:
        return candidates[0]

    return None


def classify_sale_type(
    bids_container: Tag | None,
) -> SaleType | None:
    """Classify the bidding mechanism using its actual CSS classes."""

    if bids_container is None:
        return None

    classes = set(bids_container.get("class", []))
    modes = classes.intersection({"open", "closed", "fix"})
    if len(modes) != 1:
        return None

    mode = modes.pop()
    return {
        "open": SaleType.OPEN_AUCTION,
        "closed": SaleType.CLOSED_AUCTION,
        "fix": SaleType.FIXED_PRICE,
    }[mode]


def has_hot_bid_marker(soup: BeautifulSoup) -> bool:
    """Detect the HOT BID marker."""

    if soup.select_one(".hotbid-final") is not None:
        return True

    for image in soup.find_all("img"):
        alt = str(image.get("alt", "")).casefold()

        if "hot bid" in alt:
            return True

    return False


def find_field(
    container: Tag,
    field_name: str,
) -> Tag | None:
    """Find a source field inside the bidding block."""

    for tag in container.find_all(True):
        if tag.get("id") == field_name:
            return tag

        if tag.get("name") == field_name:
            return tag

        if field_name in tag.get("class", []):
            return tag

    return None


def _fixed_price_from_json_ld(soup: BeautifulSoup) -> PriceObservation | None:
    """Use schema.org Offer only after the page explicitly marks fixed-price mode."""
    for script in soup.select('script[type="application/ld+json"]'):
        raw_json = script.string or script.get_text()
        if not raw_json:
            continue
        try:
            payload = json.loads(raw_json)
        except (TypeError, ValueError):
            continue

        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            offers = candidate.get("offers")
            offers_list = offers if isinstance(offers, list) else [offers]
            for offer in offers_list:
                if not isinstance(offer, dict) or offer.get("price") is None:
                    continue
                parsed = parse_amount(str(offer["price"]))
                if parsed is None:
                    continue
                amount, currency = parsed
                currency = str(offer.get("priceCurrency") or currency or "").upper() or None
                return PriceObservation(
                    price_type=PriceType.FIXED_PRICE,
                    amount=amount,
                    currency=currency,
                    raw_text=str(offer["price"]),
                    source_field="json-ld offers.price",
                )
    return None


def parse_vehicle_detail(
    html: str,
    source_car_id: str | None = None,
    page_url: str | None = None,
) -> ParsedVehicleDetail:
    """Parse a vehicle page without interpreting private bids as prices."""

    soup = BeautifulSoup(html, "html.parser")

    car_id = source_car_id or detect_car_id(soup, page_url)

    bids_container = find_bids_container(soup, car_id)

    sale_type = classify_sale_type(bids_container)

    bidding_classes = (
        tuple(bids_container.get("class", []))
        if bids_container is not None
        else ()
    )

    displayed_price = None
    fixed_price = None

    # This is a public displayed auction price, never a bid observation.
    if sale_type is SaleType.OPEN_AUCTION and bids_container is not None:
        price_tag = (
            find_field(bids_container, f"maximum_bid_{car_id}")
            if car_id
            else None
        )
        current_price_tag = bids_container.select_one(".current-price")
        current_price_text = clean_text(current_price_tag)
        maximum_bid_text = clean_text(price_tag)
        if current_price_text:
            raw_price = current_price_text
            source_field = "current-price"
        else:
            raw_price = maximum_bid_text
            source_field = f"maximum_bid_{car_id}" if price_tag is not None and car_id else None

        if raw_price:
            parsed = parse_amount(raw_price)
            if parsed is not None:
                amount, currency = parsed
                displayed_price = PriceObservation(
                    price_type=PriceType.DISPLAYED_AUCTION_PRICE,
                    amount=amount,
                    currency=currency,
                    raw_text=raw_price,
                    source_field=source_field or "current-price",
                )

    # A fixed price is taken only from the explicit fixed-price UI block.
    # schema.org Offer is a fallback only after the "fix" mode marker
    # has independently confirmed the sale mechanism.
    if sale_type is SaleType.FIXED_PRICE:
        fixed_block = soup.select_one(".fixed-price")
        buy_offer = fixed_block.select_one(".buy-offer") if fixed_block else None
        raw_fixed_price = None
        if buy_offer is not None:
            raw_fixed_price = (
                str(buy_offer.get("value") or "").strip()
                or clean_text(buy_offer)
            )

        if raw_fixed_price:
            parsed = parse_amount(raw_fixed_price)
            if parsed is not None:
                amount, currency = parsed
                fixed_price = PriceObservation(
                    price_type=PriceType.FIXED_PRICE,
                    amount=amount,
                    currency=currency,
                    raw_text=raw_fixed_price,
                    source_field="fixed-price .buy-offer",
                )

        if fixed_price is None:
            fixed_price = _fixed_price_from_json_ld(soup)

    return ParsedVehicleDetail(
        source_car_id=car_id,
        sale_type=sale_type,
        bidding_mode_classes=bidding_classes,
        displayed_auction_price=displayed_price,
        fixed_price=fixed_price,
        is_hot_bid=has_hot_bid_marker(soup),
    )
