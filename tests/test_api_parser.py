from decimal import Decimal

import pytest

from exleasingcar_archive.api_parser import parse_car_update_response


def test_parse_active_auction_response():
    response = """
    {
        "19714289": {
            "user_offer": 18400,
            "highest_offer": "18300",
            "actual_car_price": "0",
            "car_id": "19714289",
            "aukciono_tipas": "1",
            "car_auction_time": {
                "image": "final-call-active",
                "type": "final-call",
                "liveBidding": "0",
                "endDate": "2026-11-12 13:19:57",
                "remainingSeconds": 20
            }
        }
    }
    """

    result = parse_car_update_response(response)
    car = result["19714289"]

    assert car.source_car_id == "19714289"
    assert car.highest_offer == Decimal(18300)
    assert car.user_offer == Decimal(18400)
    assert car.actual_car_price == Decimal(0)
    assert car.final_call_active is True
    assert car.remaining_seconds == 20
    assert car.live_bidding is False


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


def test_invalid_json_raises_error():
    with pytest.raises(ValueError, match="Invalid JSON"):
        parse_car_update_response("{invalid json")
