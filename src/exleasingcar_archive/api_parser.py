"""Parse JSON updates returned by Exleasingcar's car-data endpoint."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True, slots=True)
class ParsedCarUpdate:
    source_car_id: str
    highest_offer: Decimal | None
    user_offer: Decimal | None
    actual_car_price: Decimal | None
    bid_status: str | None
    auction_type_code: str | None
    auction_phase: str | None
    final_call_active: bool
    remaining_seconds: int | None
    auction_end_at: str | None
    live_bidding: bool | None


def _optional_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _optional_decimal(
    value: Any, field_name: str, car_id: str
) -> Decimal | None:
    if value is None or value == "":
        return None

    try:
        return Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError(
            f"Invalid {field_name} for car {car_id}: {value!r}"
        ) from exc


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None

    if isinstance(value, bool):
        return value

    normalized = str(value).strip().lower()

    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False

    return None


def parse_car_update_response(
    response: str | Mapping[str, Any],
) -> dict[str, ParsedCarUpdate]:
    """Parse a response containing car IDs and their updated data."""
    if isinstance(response, str):
        try:
            payload = json.loads(response)
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON response") from exc
    else:
        payload = response

    if not isinstance(payload, Mapping):
        raise TypeError("Expected a JSON object keyed by car ID")

    result: dict[str, ParsedCarUpdate] = {}

    for raw_car_id, raw_car_data in payload.items():
        if raw_car_data is None:
            continue

        if not isinstance(raw_car_data, Mapping):
            raise TypeError(f"Expected an object for car {raw_car_id}")

        car_id = str(raw_car_id)

        raw_time = raw_car_data.get("car_auction_time")
        if raw_time is None:
            auction_time: Mapping[str, Any] = {}
        elif isinstance(raw_time, Mapping):
            auction_time = raw_time
        else:
            raise ValueError(
                f"Invalid car_auction_time for car {car_id}"
            )

        auction_phase = _optional_text(auction_time.get("type"))
        auction_image = _optional_text(auction_time.get("image"))

        result[car_id] = ParsedCarUpdate(
            source_car_id=car_id,
            highest_offer=_optional_decimal(
                raw_car_data.get("highest_offer"),
                "highest_offer",
                car_id,
            ),
            user_offer=_optional_decimal(
                raw_car_data.get("user_offer"),
                "user_offer",
                car_id,
            ),
            actual_car_price=_optional_decimal(
                raw_car_data.get("actual_car_price"),
                "actual_car_price",
                car_id,
            ),
            bid_status=_optional_text(raw_car_data.get("bid_status")),
            auction_type_code=_optional_text(
                raw_car_data.get("aukciono_tipas")
            ),
            auction_phase=auction_phase,
            final_call_active=(
                auction_phase == "final-call"
                or auction_image == "final-call-active"
            ),
            remaining_seconds=_optional_int(
                auction_time.get("remainingSeconds")
            ),
            auction_end_at=_optional_text(auction_time.get("endDate")),
            live_bidding=_optional_bool(auction_time.get("liveBidding")),
        )

    return result
