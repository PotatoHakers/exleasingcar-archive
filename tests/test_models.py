from exleasingcar_archive.models import PriceType, SaleType


def test_sale_types_are_separate():
    assert SaleType.OPEN_AUCTION.value == "open_auction"
    assert SaleType.CLOSED_AUCTION.value == "closed_auction"
    assert SaleType.FIXED_PRICE.value == "fixed_price"


def test_closing_bid_is_not_confirmed_sale_price():
    assert PriceType.CLOSING_BID != PriceType.CONFIRMED_SALE_PRICE
