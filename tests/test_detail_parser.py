
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


def test_fixed_price_sale_extracts_buy_now_price_without_treating_it_as_a_bid():
    html = """
    <div class="auto-block" car-id="19694139">
        <div class="bids active fix bids_19694139"></div>
        <div class="fixed-price">
            <input class="buy-offer" id="by_this_car_now" type="submit"
                   value="Купить сейчас за € 16 900">
        </div>
        <script type="application/ld+json">
            {"@type":"Product","offers":{"@type":"Offer",
             "price":16900,"priceCurrency":"EUR"}}
        </script>
    </div>
    """

    result = parse_vehicle_detail(html)

    assert result.source_car_id == "19694139"
    assert result.sale_type is SaleType.FIXED_PRICE
    assert result.should_monitor is False
    assert result.displayed_auction_price is None
    assert result.fixed_price is not None
    assert result.fixed_price.amount == Decimal(16900)
    assert result.fixed_price.currency == "EUR"
    assert result.fixed_price.price_type is PriceType.FIXED_PRICE
    assert result.fixed_price.source_field == "fixed-price .buy-offer"


def test_fixed_price_json_ld_is_only_a_fallback_for_confirmed_fixed_mode():
    fixed_html = """
    <div car-id="car-fixed">
        <div class="bids active fix bids_car-fixed"></div>
        <script type="application/ld+json">
            {"@type":"Product","offers":{"price":"16900","priceCurrency":"EUR"}}
        </script>
    </div>
    """
    result = parse_vehicle_detail(fixed_html)
    assert result.sale_type is SaleType.FIXED_PRICE
    assert result.fixed_price is not None
    assert result.fixed_price.amount == Decimal(16900)
    assert result.fixed_price.source_field == "json-ld offers.price"

    open_html = """
    <div car-id="car-open">
        <div class="bids active open bids_car-open"></div>
        <script type="application/ld+json">
            {"@type":"Product","offers":{"price":"16900","priceCurrency":"EUR"}}
        </script>
    </div>
    """
    open_result = parse_vehicle_detail(open_html)
    assert open_result.sale_type is SaleType.OPEN_AUCTION
    assert open_result.fixed_price is None


def test_conflicting_sale_mode_markers_are_not_guessed():
    html = """
    <div car-id="car-ambiguous">
        <div class="bids active open fix bids_car-ambiguous"></div>
    </div>
    """
    result = parse_vehicle_detail(html)
    assert result.sale_type is None
    assert result.fixed_price is None
    assert result.displayed_auction_price is None


def test_open_auction_starting_display_is_not_misclassified_as_a_bid():
    html = """
    <div car-id="car-start">
        <div class="bids active open bids_car-start">
            <div class="current-price">Начальная цена: € 7 500</div>
            <input class="my_bid_car-start" value="0">
        </div>
    </div>
    """
    result = parse_vehicle_detail(html)
    assert result.sale_type is SaleType.OPEN_AUCTION
    assert result.displayed_auction_price is not None
    assert result.displayed_auction_price.amount == Decimal(7500)
    assert result.displayed_auction_price.price_type is PriceType.DISPLAYED_AUCTION_PRICE
