from decimal import Decimal

from exleasingcar_archive.dashboard import DashboardHandler
from exleasingcar_archive.database import ArchiveDatabase
from exleasingcar_archive.models import PriceType, SaleType
from exleasingcar_archive.parsers import PriceObservation


def test_dashboard_exposes_sale_type_and_fixed_price_without_bid_mixing(tmp_path):
    database = ArchiveDatabase(tmp_path / "dashboard.sqlite3")
    database.initialize()
    database.upsert_vehicle("car-1", displayed_title="Audi A6")
    database.upsert_auction_lot("car-1", "auction-1", sale_type=SaleType.FIXED_PRICE)
    database.record_lot_price_observation(
        "car-1",
        PriceObservation(
            price_type=PriceType.FIXED_PRICE,
            amount=Decimal(16900),
            currency="EUR",
            raw_text="Купить сейчас за € 16 900",
            source_field="fixed-price .buy-offer",
        ),
        exleasingcar_auction_id="auction-1",
        sale_type=SaleType.FIXED_PRICE,
    )

    handler = object.__new__(DashboardHandler)
    handler.database_path = database.path
    payload = handler._load_data()

    assert payload["schema_version"] == 5
    assert payload["counts"]["lot_price_observations"] == 1
    assert payload["lots"][0]["sale_type"] == "fixed_price"
    assert payload["lot_prices"][0]["price_type"] == "fixed_price"
    assert payload["lot_prices"][0]["amount"] == "16900"
    assert payload["bids"] == []
