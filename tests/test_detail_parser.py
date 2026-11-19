
from decimal import Decimal
from pathlib import Path

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

    assert result.displayed_auction_price.source_field == "current-price"
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


def test_conflicting_open_and_closed_sale_markers_are_not_guessed():
    html = """
    <div car-id="car-ambiguous">
        <div class="bids active open closed fix bids_car-ambiguous"></div>
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



def test_real_catalogue_detail_layout_uses_public_current_price_not_user_bid_fields():
    # Minimized/sanitized structure copied from a real 17 Nov 2026 detail page.
    # The empty maximum_bid span and user-only input fields must not override
    # the visible public "Текущая цена" value.
    html = """
    <div car-id="19740672">
        <div class="bids active open bids_19740672">
            <div class="current-price">Текущая цена: € 11 600</div>
            <span class="maximum_bid_19740672"></span>
            <input class="maximum_bid2_19740672" value="11600">
            <input class="my_bid2_19740672" value="0">
            <input class="v_user_offer_max_19740672" value="0">
            <input class="bid_19740672" value="11700">
        </div>
    </div>
    """

    result = parse_vehicle_detail(html)

    assert result.sale_type is SaleType.OPEN_AUCTION
    assert result.displayed_auction_price is not None
    assert result.displayed_auction_price.amount == Decimal(11600)
    assert result.displayed_auction_price.currency == "EUR"
    assert result.displayed_auction_price.source_field == "current-price"


def test_maximum_bid_span_is_used_when_current_price_text_is_absent():
    html = """
    <div car-id="car-max">
        <div class="bids active open bids_car-max">
            <div class="current-price"></div>
            <span class="maximum_bid_car-max">€ 12 300</span>
        </div>
    </div>
    """

    result = parse_vehicle_detail(html)

    assert result.displayed_auction_price is not None
    assert result.displayed_auction_price.amount == Decimal(12300)
    assert result.displayed_auction_price.source_field == "maximum_bid_car-max"


def test_open_auction_without_public_price_does_not_create_price_observation():
    html = """
    <div class="auto-block" car-id="car-no-price">
        <div class="bids active open bids_car-no-price">
            <input class="maximum_bid_car-no-price" value="">
        </div>
    </div>
    """

    result = parse_vehicle_detail(
        html,
        source_car_id="car-no-price",
    )

    assert result.sale_type is SaleType.OPEN_AUCTION
    assert result.displayed_auction_price is None



def test_open_auction_with_fixed_price_option_extracts_public_current_price():
    fixture = (
        Path(__file__).parent
        / "fixtures"
        / "vehicle_detail_open_fixed_option.html"
    )
    html = fixture.read_text(encoding="utf-8")

    result = parse_vehicle_detail(html)

    assert result.source_car_id == "19764527"
    assert result.sale_type is SaleType.OPEN_AUCTION
    assert {"open", "fix"}.issubset(set(result.bidding_mode_classes))
    assert result.displayed_auction_price is not None
    assert result.displayed_auction_price.amount == Decimal(9900)
    assert result.displayed_auction_price.currency == "EUR"
    assert result.displayed_auction_price.source_field == "current-price"
    assert result.fixed_price is None


def test_closed_auction_with_fixed_price_option_keeps_closed_type():
    html = """
    <div class="auto-block" car-id="car-closed-fix">
        <div class="bids active closed fix bids_car-closed-fix">
            <div class="current-price">Текущая цена: € 8 500</div>
        </div>
    </div>
    """

    result = parse_vehicle_detail(html)

    assert result.sale_type is SaleType.CLOSED_AUCTION
    assert result.displayed_auction_price is None
    assert result.fixed_price is None
