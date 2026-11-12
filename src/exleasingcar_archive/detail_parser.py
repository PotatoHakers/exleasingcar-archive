
"""Parser for individual Exleasingcar vehicle pages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from bs4 import BeautifulSoup, Tag

from .models import PriceType
from .parsers import PriceObservation, clean_text, parse_amount


@dataclass(frozen=True, slots=True)
class ParsedVehicleDetail:
    """Values extracted from an individual vehicle page."""

    source_car_id: str | None

    # Raw source value. Not automatically treated as a leading bid.
    maximum_bid_value: Decimal | None
    maximum_bid_currency: str | None
    maximum_bid_raw: str | None

    # The user's own bid, explicitly separated from market prices.
    own_bid: PriceObservation | None

    # Value entered in the bid form, not an actual market observation.
    bid_input_amount: Decimal | None
    bid_input_currency: str | None
    bid_input_raw: str | None


def find_field(
    soup: BeautifulSoup,
    field_name: str,
) -> Tag | None:
    """Find a field by its id, name or CSS class."""

    for tag in soup.find_all(True):
        if tag.get("id") == field_name:
            return tag

        if tag.get("name") == field_name:
            return tag

        classes = tag.get("class", [])

        if field_name in classes:
            return tag

    return None


def read_field_value(tag: Tag | None) -> str | None:
    """Read a value from an input or a visible HTML element."""

    if tag is None:
        return None

    if tag.has_attr("value"):
        value = str(tag.get("value", "")).strip()
        return value or None

    if tag.has_attr("data-value"):
        value = str(tag.get("data-value", "")).strip()
        return value or None

    return clean_text(tag)


def detect_car_id(
    soup: BeautifulSoup,
    page_url: str | None,
) -> str | None:
    """Detect vehicle ID from URL or known field names."""

    if page_url:
        match = re.search(r"/auto-details/(\d+)", page_url)

        if match:
            return match.group(1)

    pattern = re.compile(
        r"(?:maximum_bid2|my_bid2|bid)_(\d+)"
    )

    for tag in soup.find_all(True):
        attributes = [
            tag.get("id"),
            tag.get("name"),
            *tag.get("class", []),
        ]

        for attribute in attributes:
            if attribute is None:
                continue

            match = pattern.fullmatch(str(attribute))

            if match:
                return match.group(1)

    return None


def extract_money(
    raw_value: str | None,
) -> tuple[Decimal | None, str | None]:
    """Parse a monetary value while retaining its original source separately."""

    if raw_value is None:
        return None, None

    parsed = parse_amount(raw_value)

    if parsed is None:
        return None, None

    return parsed


def parse_vehicle_detail(
    html: str,
    source_car_id: str | None = None,
    page_url: str | None = None,
) -> ParsedVehicleDetail:
    """Parse known price-related fields from a vehicle detail page."""

    soup = BeautifulSoup(html, "html.parser")

    car_id = source_car_id or detect_car_id(soup, page_url)

    if car_id is None:
        return ParsedVehicleDetail(
            source_car_id=None,
            maximum_bid_value=None,
            maximum_bid_currency=None,
            maximum_bid_raw=None,
            own_bid=None,
            bid_input_amount=None,
            bid_input_currency=None,
            bid_input_raw=None,
        )

    # 1. maximum_bid2: preserve its source meaning.
    maximum_bid_tag = find_field(
        soup,
        f"maximum_bid2_{car_id}",
    )
    maximum_bid_raw = read_field_value(maximum_bid_tag)

    maximum_bid_value, maximum_bid_currency = extract_money(
        maximum_bid_raw
    )

    # 2. my_bid2: the user's own bid.
    own_bid_tag = find_field(
        soup,
        f"my_bid2_{car_id}",
    )
    own_bid_raw = read_field_value(own_bid_tag)

    own_bid = None

    parsed_own_bid = extract_money(own_bid_raw)

    if own_bid_raw is not None and parsed_own_bid[0] is not None:
        own_amount, own_currency = parsed_own_bid

        own_bid = PriceObservation(
            price_type=PriceType.OWN_BID,
            amount=own_amount,
            currency=own_currency,
            raw_text=own_bid_raw,
            source_field=f"my_bid2_{car_id}",
        )

    # 3. bid: form input, not an actual placed bid.
    bid_input_tag = find_field(
        soup,
        f"bid_{car_id}",
    )
    bid_input_raw = read_field_value(bid_input_tag)

    bid_input_amount, bid_input_currency = extract_money(
        bid_input_raw
    )

    return ParsedVehicleDetail(
        source_car_id=car_id,
        maximum_bid_value=maximum_bid_value,
        maximum_bid_currency=maximum_bid_currency,
        maximum_bid_raw=maximum_bid_raw,
        own_bid=own_bid,
        bid_input_amount=bid_input_amount,
        bid_input_currency=bid_input_currency,
        bid_input_raw=bid_input_raw,
    )
