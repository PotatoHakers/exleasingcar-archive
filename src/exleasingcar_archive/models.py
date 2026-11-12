"""Core domain types for Exleasingcar listings."""

from enum import Enum


class SaleType(str, Enum):
    OPEN_AUCTION = "open_auction"
    CLOSED_AUCTION = "closed_auction"
    FIXED_PRICE = "fixed_price"
    DISPLAYED_AUCTION_PRICE = "displayed_auction_price"


class AuctionStatus(str, Enum):
    ACTIVE = "active"
    FINAL_CALL = "final_call"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class VehicleStatus(str, Enum):
    ACTIVE = "active"
    HOT_BID = "hot_bid"
    FINAL_CALL = "final_call"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class PriceType(str, Enum):
    CURRENT_PRICE = "current_price"
    LEADING_BID = "leading_bid"
    OWN_BID = "own_bid"
    FINAL_CALL_PRICE = "final_call_price"
    CLOSING_BID = "closing_bid"
    FIXED_PRICE = "fixed_price"
    CONFIRMED_SALE_PRICE = "confirmed_sale_price"
    DISPLAYED_AUCTION_PRICE = "displayed_auction_price"
