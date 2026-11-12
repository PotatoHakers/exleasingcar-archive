
"""Parsers for saved Exleasingcar HTML pages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from .models import AuctionStatus, PriceType, VehicleStatus


@dataclass(frozen=True, slots=True)
class PriceObservation:
    """Price extracted from one source field."""

    price_type: PriceType
    amount: Decimal
    currency: str | None
    raw_text: str
    source_field: str


@dataclass(frozen=True, slots=True)
class ParsedLot:
    """Vehicle found on an auction page."""

    source_car_id: str
    title: str | None
    source_url: str | None
    status: VehicleStatus
    raw_status: str | None
    is_hot_bid: bool
    current_price: PriceObservation | None


@dataclass(frozen=True, slots=True)
class ParsedAuctionPage:
    """Parsed auction page."""

    source_auction_id: str | None
    status: AuctionStatus
    raw_status: str | None
    lots: tuple[ParsedLot, ...]


CURRENCIES = ("EUR", "USD", "GBP", "PLN", "CZK", "SEK", "NOK", "DKK", "CHF")


def clean_text(tag: Tag | None) -> str | None:
    if tag is None:
        return None

    text = " ".join(tag.get_text(" ", strip=True).split())
    return text or None


def parse_amount(raw_text: str) -> tuple[Decimal, str | None] | None:
    """Parse a monetary amount without using float."""

    currency_match = re.search(
        rf"\b({'|'.join(CURRENCIES)})\b", raw_text.upper()
    )
    currency = currency_match.group(1) if currency_match else None

    if currency is None and "€" in raw_text:
        currency = "EUR"
    elif currency is None and "£" in raw_text:
        currency = "GBP"

    candidates = re.findall(
        r"[-+]?\d[\d\s\u00a0\u202f.,]*", raw_text
    )

    if not candidates:
        return None

    number = max(candidates, key=len).strip()
    number = re.sub(r"\s+", "", number)

    if not number:
        return None

    if "," in number and "." in number:
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")

    elif "," in number:
        last_group = number.rsplit(",", 1)[1]
        if len(last_group) in (1, 2):
            number = number.replace(",", ".")
        else:
            number = number.replace(",", "")

    elif "." in number:
        last_group = number.rsplit(".", 1)[1]
        if len(last_group) not in (1, 2):
            number = number.replace(".", "")

    try:
        amount = Decimal(number)
    except InvalidOperation:
        return None

    if not amount.is_finite():
        return None

    return amount, currency


def get_car_id(tag: Tag) -> str | None:
    car_id = tag.get("car-id")

    if car_id:
        return str(car_id).strip()

    element_id = str(tag.get("id", ""))
    match = re.fullmatch(r"auto_(\d+)", element_id)

    return match.group(1) if match else None


def find_car_field(
    card: Tag,
    prefix: str,
    car_id: str,
) -> Tag | None:
    target_class = f"{prefix}{car_id}"

    for element in [card, *card.find_all(class_=True)]:
        classes = element.get("class", [])

        if target_class in classes:
            return element

    return None


def extract_title(card: Tag) -> str | None:
    selectors = (
        ".car-title",
        "[class*='car-title']",
        "h1",
        "h2",
        "h3",
        "h4",
    )

    for selector in selectors:
        title = clean_text(card.select_one(selector))

        if title:
            return title

    detail_link = card.find(
        "a",
        href=re.compile(r"/ru/auto-details/\d+"),
    )

    return clean_text(detail_link)


def extract_detail_url(
    card: Tag,
    page_url: str | None,
) -> str | None:
    link = card.find(
        "a",
        href=re.compile(r"/ru/auto-details/\d+"),
    )

    if link is None:
        return None

    href = link.get("href")

    if not href:
        return None

    base_url = page_url or "https://www.exleasingcar.com"

    return urljoin(base_url, str(href))


def has_hot_bid_marker(card: Tag) -> bool:
    if card.select_one(".hotbid-final") is not None:
        return True

    for image in card.find_all("img"):
        alt = str(image.get("alt", "")).casefold()
        src = str(image.get("src", "")).casefold()

        if "hot bid" in alt or "final-call.gif" in src:
            return True

    return False


def classify_vehicle_status(
    raw_status: str | None,
    is_hot_bid: bool,
) -> VehicleStatus:
    text = (raw_status or "").casefold()

    if any(
        marker in text
        for marker in (
            "аукцион закончился",
            "торги завершены",
            "auction ended",
            "completed",
        )
    ):
        return VehicleStatus.COMPLETED

    if "final call" in text:
        return VehicleStatus.FINAL_CALL

    if is_hot_bid:
        return VehicleStatus.HOT_BID

    if any(
        marker in text
        for marker in (
            "активен",
            "торги идут",
            "active",
            "live",
        )
    ):
        return VehicleStatus.ACTIVE

    return VehicleStatus.UNKNOWN


def extract_auction_status(
    soup: BeautifulSoup,
) -> tuple[AuctionStatus, str | None]:
    for tag in soup.find_all(("h1", "h2", "button")):
        text = clean_text(tag)

        if not text:
            continue

        lowered = text.casefold()

        if "final call" in lowered:
            return AuctionStatus.FINAL_CALL, text

        if any(
            marker in lowered
            for marker in (
                "аукцион завершён",
                "аукцион завершен",
                "auction completed",
            )
        ):
            return AuctionStatus.COMPLETED, text

        if any(
            marker in lowered
            for marker in (
                "аукцион активен",
                "auction active",
            )
        ):
            return AuctionStatus.ACTIVE, text

    return AuctionStatus.UNKNOWN, None


def parse_auction_list(
    html: str,
    page_url: str | None = None,
) -> ParsedAuctionPage:
    """Parse auction information and vehicle cards from HTML."""

    soup = BeautifulSoup(html, "html.parser")

    # Auction ID
    auction_input = soup.find(
        "input",
        attrs={"name": "aukciono_id"},
    )

    auction_id = None

    if isinstance(auction_input, Tag):
        value = auction_input.get("value")

        if value:
            auction_id = str(value).strip()

    if auction_id is None and page_url:
        match = re.search(r"auction-(\d+)", page_url)

        if match:
            auction_id = match.group(1)

    # Auction status
    auction_status, raw_auction_status = extract_auction_status(soup)

    # Find vehicle cards
    cards_by_id: dict[str, Tag] = {}

    for candidate in soup.select("[car-id], [id^='auto_']"):
        car_id = get_car_id(candidate)

        if car_id:
            cards_by_id.setdefault(car_id, candidate)

    lots: list[ParsedLot] = []

    for car_id, card in cards_by_id.items():

        # Vehicle status
        status_tag = find_car_field(
            card,
            "car_auction_time_",
            car_id,
        )

        raw_status = clean_text(status_tag)
        is_hot_bid = has_hot_bid_marker(card)

        vehicle_status = classify_vehicle_status(
            raw_status,
            is_hot_bid,
        )

        # Current displayed price
        price_tag = find_car_field(
            card,
            "maximum_bid_",
            car_id,
        )

        current_price = None
        raw_price = clean_text(price_tag)

        if raw_price:
            parsed_amount = parse_amount(raw_price)

            if parsed_amount is not None:
                amount, currency = parsed_amount

                current_price = PriceObservation(
                    price_type=PriceType.CURRENT_PRICE,
                    amount=amount,
                    currency=currency,
                    raw_text=raw_price,
                    source_field=f"maximum_bid_{car_id}",
                )

        lots.append(
            ParsedLot(
                source_car_id=car_id,
                title=extract_title(card),
                source_url=extract_detail_url(card, page_url),
                status=vehicle_status,
                raw_status=raw_status,
                is_hot_bid=is_hot_bid,
                current_price=current_price,
            )
        )

    return ParsedAuctionPage(
        source_auction_id=auction_id,
        status=auction_status,
        raw_status=raw_auction_status,
        lots=tuple(lots),
    )
