
from decimal import Decimal
from pathlib import Path

import pytest

from exleasingcar_archive.api_parser import parse_car_update_response

FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "active_final_call_19714289.json"
)


def test_parse_active_auction_response_fixture():
    response = FIXTURE_PATH.read_text(encoding="utf-8")
    result = parse_car_update_response(response)

    car = result["19714289"]

    assert car.source_car_id == "19714289"
    assert car.highest_offer == Decimal(18300)
    assert car.user_offer == Decimal(18400)
    assert car.actual_car_price == Decimal(0)
    assert car.auction_type_code == "1"
    assert car.auction_phase == "final-call"
    assert car.final_call_active is True
    assert car.remaining_seconds == 20
    assert car.auction_end_at == "2026-11-12 13:19:57"
    assert car.live_bidding is False
    assert car.bid_status is None


def test_zero_price_is_preserved():
    response = {
        "12345": {
            "highest_offer": "0",
            "actual_car_price": "0",
        }
    }

    car = parse_car_update_response(response)["12345"]

    assert car.highest_offer == Decimal(0)
    assert car.actual_car_price == Decimal(0)


def test_empty_auction_time_is_allowed():
    response = {
        "12345": {
            "highest_offer": "15000",
        }
    }

    car = parse_car_update_response(response)["12345"]

    assert car.highest_offer == Decimal(15000)
    assert car.final_call_active is False
    assert car.remaining_seconds is None


def test_null_car_entries_are_skipped():
    response = {
        "12345": {
            "highest_offer": "15000",
        },
        "67890": None,
    }

    result = parse_car_update_response(response)

    assert "12345" in result
    assert "67890" not in result


def test_multiple_cars_are_parsed():
    response = {
        "11111": {"highest_offer": "12000"},
        "22222": {"highest_offer": "15000"},
    }

    result = parse_car_update_response(response)

    assert len(result) == 2
    assert result["11111"].highest_offer == Decimal(12000)
    assert result["22222"].highest_offer == Decimal(15000)


def test_invalid_json_raises_error():
    with pytest.raises(ValueError, match="Invalid JSON"):
        parse_car_update_response("{invalid json")
