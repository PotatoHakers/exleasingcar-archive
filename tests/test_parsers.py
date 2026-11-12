
from decimal import Decimal

from exleasingcar_archive.models import (
    AuctionStatus,
    PriceType,
    VehicleStatus,
)
from exleasingcar_archive.parsers import parse_auction_list


def test_parse_auction_page_and_keep_price_types_separate():
    html = """
    <html>
    <body>
        <input type="hidden" name="aukciono_id" value="210411">

        <h2>
            <button>FINAL CALL</button>
        </h2>

        <div class="car-card"
             car-id="19681344"
             id="auto_19681344">

            <h3>
                <a href="/ru/auto-details/19681344">
                    BMW 320d
                </a>
            </h3>

            <span class="maximum_bid_19681344">
                6 100 EUR
            </span>

            <div class="car_auction_time_19681344">
                Аукцион закончился!
            </div>

            <input id="my_bid2_19681344" value="5000">
            <input id="bid_19681344" value="6200">

        </div>

        <div class="car-card"
             car-id="19681351"
             id="auto_19681351">

            <h3>
                <a href="/ru/auto-details/19681351">
                    Audi A4
                </a>
            </h3>

            <img
                class="hotbid-final"
                src="/images/final-call.gif"
                alt="HOT BID"
            >

        </div>
    </body>
    </html>
    """

    result = parse_auction_list(
        html,
        "https://www.exleasingcar.com/ru/auction-210411",
    )

    assert result.source_auction_id == "210411"
    assert result.status is AuctionStatus.FINAL_CALL
    assert len(result.lots) == 2

    bmw = result.lots[0]

    assert bmw.source_car_id == "19681344"
    assert bmw.title == "BMW 320d"
    assert bmw.source_url == (
        "https://www.exleasingcar.com/ru/auto-details/19681344"
    )
    assert bmw.status is VehicleStatus.COMPLETED

    assert bmw.current_price is not None
    assert bmw.current_price.amount == Decimal("6100")
    assert bmw.current_price.currency == "EUR"
    assert bmw.current_price.price_type is PriceType.CURRENT_PRICE

    audi = result.lots[1]

    assert audi.status is VehicleStatus.HOT_BID
    assert audi.is_hot_bid is True


def test_auction_id_can_be_read_from_url():
    html = """
    <html>
        <body>
            <div car-id="12345" id="auto_12345"></div>
        </body>
    </html>
    """

    result = parse_auction_list(
        html,
        "https://www.exleasingcar.com/ru/auction-98765",
    )

    assert result.source_auction_id == "98765"
    assert len(result.lots) == 1


def test_unknown_price_is_not_guessed():
    html = """
    <div car-id="123" id="auto_123">
        <span class="maximum_bid_123">
            Price unavailable
        </span>
    </div>
    """

    result = parse_auction_list(html)

    assert result.lots[0].current_price is None
