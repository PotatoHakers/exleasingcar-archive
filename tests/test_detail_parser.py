
from decimal import Decimal

from exleasingcar_archive.detail_parser import parse_vehicle_detail
from exleasingcar_archive.models import PriceType, SaleType


def test_open_auction_price_is_extracted():
    html = """
    <div class="auto-block" car-id="19686567">
        <div class="bids active open bids_19686567">

            <div class="current-price">
                Текущая цена:
                <span>€</span>
                <span class="maximum_bid_19686567">63 900</span>
            </div>

            <input
                class="maximum_bid2_19686567"
                value="63900"
            >

            <input
                class="my_bid2_19686567"
                value="0"
            >

            <input
                class="bid_19686567"
                value="64000"
            >

        </div>

        <img class="hotbid-final" alt="HOT BID">
    </div>
    """

    result = parse_vehicle_detail(html)

    assert result.source_car_id == "19686567"
    assert result.sale_type is SaleType.OPEN_AUCTION
    assert result.should_monitor is True

    assert result.displayed_auction_price is not None
    assert result.displayed_auction_price.amount == Decimal(63900)
    assert result.displayed_auction_price.currency == "EUR"

    assert (
        result.displayed_auction_price.price_type
        is PriceType.DISPLAYED_AUCTION_PRICE
    )

    assert result.is_hot_bid is True


def test_closed_auction_is_excluded():
    html = """
    <div class="auto-block" car-id="19706787">
        <div class="bids active closed bids_19706787">

            <div class="my-offer-text">
                Ставка без учёта НДС
            </div>

            <input
                class="bid_19706787"
                value="0"
            >

            <input
                type="submit"
                value="Делать ставку"
                onclick="makeOfferClosed(19706787,2)"
            >

        </div>
    </div>
    """

    result = parse_vehicle_detail(html)

    assert result.sale_type is SaleType.CLOSED_AUCTION
    assert result.should_monitor is False
    assert result.displayed_auction_price is None


def test_unknown_bidding_mode_is_excluded():
    html = """
    <div class="auto-block" car-id="12345">
        <h1>Test vehicle</h1>
    </div>
    """

    result = parse_vehicle_detail(html)

    assert result.source_car_id == "12345"
    assert result.sale_type is None
    assert result.should_monitor is False
    assert result.displayed_auction_price is None


def test_open_auction_without_visible_price_is_not_guessed():
    html = """
    <div class="auto-block" car-id="54321">
        <div class="bids active open bids_54321">
            <input class="bid_54321" value="5000">
        </div>
    </div>
    """

    result = parse_vehicle_detail(html)

    assert result.sale_type is SaleType.OPEN_AUCTION
    assert result.should_monitor is True
    assert result.displayed_auction_price is None
