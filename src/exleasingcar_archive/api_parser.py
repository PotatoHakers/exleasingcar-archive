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
    minimum_price: Decimal | None
    indicative_price: Decimal | None
    user_offer: Decimal | None
    actual_car_price: Decimal | None
    bid_status: str | None
    auction_type_code: str | None
    auction_phase: str | None
    final_call_active: bool
    remaining_seconds: int | None
    auction_end_at: str | None
    live_bidding: bool | None

    @property
    def has_positive_highest_offer(self) -> bool:
        """A zero/negative API placeholder is never a candidate bid."""
        return self.highest_offer is not None and self.highest_offer > 0

    @property
    def completion_reason(self) -> str | None:
        """Return a source-confirmed end state for this lot, not its parent auction."""
        # A positive live-bidding signal is authoritative even if a stale
        # countdown has reached zero. Conversely, FINAL CALL must not keep a
        # lot alive after the API explicitly reports bidding disabled and its
        # per-lot countdown has expired.
        if self.live_bidding is True:
            return None

        terminal_values = {"closed", "completed", "ended", "finished"}
        phase = (self.auction_phase or "").strip().casefold().replace("_", "-")
        status = (self.bid_status or "").strip().casefold().replace("_", "-")
        if phase in terminal_values or status in terminal_values:
            return "api_terminal_status"
        # At exactly zero the site may still be rendering the FINAL CALL
        # transition. Require a negative timer before overriding that flag.
        if self.final_call_active and (
            self.remaining_seconds is None or self.remaining_seconds >= 0
        ):
            return None
        if (
            self.remaining_seconds is not None
            and self.remaining_seconds <= 0
            and self.live_bidding is False
        ):
            return "countdown_expired"

        if self.final_call_active:
            return None
        return None


def _optional_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _optional_decimal(value: Any, field_name: str, car_id: str) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError(f"Invalid {field_name} for car {car_id}: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"Invalid {field_name} for car {car_id}: {value!r}")
    return parsed


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
    """Parse a response containing car IDs and their updated data.

    The endpoint returns a mapping keyed by car ID. In the captured response
    used by this project, the countdown is top-level ``liko_s`` and the
    end-time text is nested under ``car_auction_time.laikas``. Older/alternate
    response shapes are supported as fallbacks.
    """
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
        embedded_car_id = _optional_text(raw_car_data.get("car_id"))
        if embedded_car_id is not None and embedded_car_id != car_id:
            raise ValueError(
                f"Response key does not match embedded car_id: {car_id!r} != "
                f"{embedded_car_id!r}"
            )

        raw_time = raw_car_data.get("car_auction_time")
        if raw_time is None:
            auction_time: Mapping[str, Any] = {}
        elif isinstance(raw_time, Mapping):
            auction_time = raw_time
        else:
            raise ValueError(f"Invalid car_auction_time for car {car_id}")

        auction_phase = _optional_text(auction_time.get("type"))
        auction_image = _optional_text(auction_time.get("image"))

        remaining_seconds = _optional_int(raw_car_data.get("liko_s"))
        if remaining_seconds is None:
            remaining_seconds = _optional_int(auction_time.get("remainingSeconds"))

        auction_end_at = _optional_text(auction_time.get("laikas"))
        if auction_end_at is None:
            auction_end_at = _optional_text(auction_time.get("endDate"))

        result[car_id] = ParsedCarUpdate(
            source_car_id=car_id,
            highest_offer=_optional_decimal(
                raw_car_data.get("highest_offer"), "highest_offer", car_id
            ),
            minimum_price=_optional_decimal(
                raw_car_data.get("text_minimali_kaina"), "text_minimali_kaina", car_id
            ),
            indicative_price=_optional_decimal(
                raw_car_data.get("text_orientacine_kaina"),
                "text_orientacine_kaina",
                car_id,
            ),
            user_offer=_optional_decimal(raw_car_data.get("user_offer"), "user_offer", car_id),
            actual_car_price=_optional_decimal(
                raw_car_data.get("actual_car_price"), "actual_car_price", car_id
            ),
            bid_status=_optional_text(raw_car_data.get("bid_status")),
            auction_type_code=_optional_text(raw_car_data.get("aukciono_tipas")),
            auction_phase=auction_phase,
            final_call_active=(
                auction_phase == "final-call" or auction_image == "final-call-active"
            ),
            remaining_seconds=remaining_seconds,
            auction_end_at=auction_end_at,
            live_bidding=_optional_bool(auction_time.get("liveBidding")),
        )

    return result
