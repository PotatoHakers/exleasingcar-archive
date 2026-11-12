
from decimal import Decimal

from exleasingcar_archive.models import PriceType
from exleasingcar_archive.detail_parser import parse_vehicle_detail


def test_parse_vehicle_detail_fields():
    html = """
    <html>
    <body>
        <input
            type="hidden"
            id="maximum_bid2_19686567"
            value="63900"
        >

        <input
            type="hidden"
            id="my_bid2_19686567"
            value="0"
        >

        <input
            type="number"
            id="bid_19686567"
            value="64000"
        >
    </body>
    </html>
    """

    result = parse_vehicle_detail(
        html,
        page_url=(
            "https://www.exleasingcar.com/ru/"
            "auto-details/19686567"
        ),
    )

    assert result.source_car_id == "19686567"

    assert result.maximum_bid_value == Decimal("63900")

    assert result.own_bid is not None
    assert result.own_bid.amount == Decimal("0")
    assert result.own_bid.price_type is PriceType.OWN_BID

    assert result.bid_input_amount == Decimal("64000")


def test_form_input_is_not_treated_as_actual_bid():
    html = """
    <input
        id="bid_12345"
        value="64000"
    >
    """

    result = parse_vehicle_detail(html)

    assert result.bid_input_amount == Decimal("64000")
    assert result.own_bid is None
    assert result.maximum_bid_value is None


def test_unknown_values_are_not_guessed():
    html = """
    <div id="maximum_bid2_12345">
        Price unavailable
    </div>
    """

    result = parse_vehicle_detail(html)

    assert result.source_car_id == "12345"
    assert result.maximum_bid_value is None
    assert result.maximum_bid_raw == "Price unavailable"
