import pytest

from exleasingcar_archive.collector import (
    ExleasingcarCollector,
    build_argument_parser,
    extract_vehicle_detail_urls,
    filter_updates_for_catalog,
)


def test_save_html_snapshots_can_be_disabled(tmp_path):
    collector = ExleasingcarCollector(
        database=None,
        raw_dir=tmp_path,
        save_html_snapshots=False,
    )
    snapshot = tmp_path / "auction" / "vehicle.html"

    collector._save_html(snapshot, "<p>account-specific content</p>")

    assert not snapshot.exists()
    assert not snapshot.parent.exists()


def test_save_html_snapshots_are_disabled_by_default(tmp_path):
    collector = ExleasingcarCollector(database=None, raw_dir=tmp_path)
    snapshot = tmp_path / "auction" / "vehicle.html"

    collector._save_html(snapshot, "<p>public vehicle content</p>")

    assert not snapshot.exists()


def test_save_html_snapshots_redact_account_and_private_form_values(tmp_path):
    collector = ExleasingcarCollector(
        database=None,
        raw_dir=tmp_path,
        save_html_snapshots=True,
    )
    snapshot = tmp_path / "auction" / "vehicle.html"
    html = """
    <div class="module-user">Alex Example · ID вашей учетной записи: 407930</div>
    <script>
      var __USER_ID__ = '407930';
      var __EMAIL__ = 'owner@example.com';
      OneSignal.sendTag("ID", "407930");
    </script>
    <div class="vehicle">
      <p>Vehicle ID: 19810183</p>
      <p>Chassis: WAUZZZF48NA0*****</p>
      <input name="user_offer" value="12345">
      <input id="bid_step_max" value="25000">
      <textarea name="note">Private account note</textarea>
      <input name="public_vehicle_price" value="18900">
    </div>
    """

    collector._save_html(snapshot, html)
    saved = snapshot.read_text(encoding="utf-8")

    assert "Alex Example" not in saved
    assert "407930" not in saved
    assert "owner@example.com" not in saved
    assert "12345" not in saved
    assert "25000" not in saved
    assert "Private account note" not in saved
    assert "19810183" in saved
    assert "WAUZZZF48NA0*****" in saved
    assert "18900" in saved


def test_collector_cli_snapshot_retention_is_opt_in_and_legacy_opt_out_still_works():
    default_args = build_argument_parser().parse_args([])
    save_args = build_argument_parser().parse_args(["--save-html-snapshots"])
    legacy_args = build_argument_parser().parse_args(["--no-html-snapshots"])

    assert default_args.save_html_snapshots is False
    assert save_args.save_html_snapshots is True
    assert legacy_args.save_html_snapshots is False


def test_collector_cli_supports_no_time_limit_without_changing_resource_defaults():
    default_args = build_argument_parser().parse_args([])
    unlimited_args = build_argument_parser().parse_args(["--no-time-limit"])

    assert default_args.no_time_limit is False
    assert default_args.max_run_seconds == 900
    assert unlimited_args.no_time_limit is True
    assert unlimited_args.max_detail_pages_per_run == 60
    assert unlimited_args.max_live_lots_per_run == 500


def test_filter_updates_for_catalog_rejects_other_auction_ids():
    updates = {
        "car-1": {"highest_offer": 100},
        "car-2": {"highest_offer": 200},
        "car-3": {"highest_offer": 300},
    }

    filtered = filter_updates_for_catalog(updates, {"car-1", "car-3"})

    assert list(filtered) == ["car-1", "car-3"]
    assert filtered["car-1"]["highest_offer"] == 100


def test_extract_vehicle_detail_urls_uses_card_links_and_resolves_relative_urls():
    html = """
    <div class="s-car-list" car-id="19655238">
        <a class="box-link" href="/ru/auto-details/19655238">open</a>
    </div>
    <div class="s-car-list" car-id="19714028">
        <a href="https://www.exleasingcar.com/ru/auto-details/19714028">open</a>
    </div>
    <div class="s-car-list" car-id="19718550">
        <span>No detail link</span>
    </div>
    """

    links = extract_vehicle_detail_urls(html, "https://www.exleasingcar.com/ru/auction-1/")

    assert links == {
        "19655238": "https://www.exleasingcar.com/ru/auto-details/19655238",
        "19714028": "https://www.exleasingcar.com/ru/auto-details/19714028",
    }


def test_extract_vehicle_detail_urls_deduplicates_vehicle_cards():
    html = """
    <div car-id="car-1"><a href="/auto-details/car-1">one</a></div>
    <div car-id="car-1"><a href="/auto-details/car-1">duplicate</a></div>
    """

    links = extract_vehicle_detail_urls(html, "https://www.exleasingcar.com")

    assert links == {"car-1": "https://www.exleasingcar.com/auto-details/car-1"}


def test_api_response_recorder_accepts_only_target_endpoint_json():
    from exleasingcar_archive.collector import ApiResponseRecorder

    class FakeResponse:
        url = "https://www.exleasingcar.com/ajax_update_car_form.v3.1.php"
        ok = True
        status = 200

        def finished(self):
            return None

        def body(self):
            return b'{"car-1": {"highest_offer": 123}}'

    recorder = ApiResponseRecorder()
    recorder._on_response(FakeResponse())

    assert len(recorder.responses) == 1
    assert recorder.responses[0].payload["car-1"]["highest_offer"] == 123


def test_api_response_recorder_ignores_non_target_urls():
    from exleasingcar_archive.collector import ApiResponseRecorder

    class FakeResponse:
        url = "https://www.exleasingcar.com/ajax_other.php"
        ok = True
        status = 200

    recorder = ApiResponseRecorder()
    recorder._on_response(FakeResponse())

    assert recorder.responses == []



def test_extract_vehicle_detail_urls_from_card_onclick_after_login_redirect():
    html = """
    <div class="s-car-list auto-block" car-id="19694139"
         onclick="javascript:$('.after_login_redirect').val('https://www.exleasingcar.com/ru/auto-details/19694139'); open_lightbox('auto_19694139');">
        <a href="javascript:void(0);">open</a>
    </div>
    <div class="s-car-list auto-block" car-id="19521373"
         onclick="javascript:$('.after_login_redirect').val('https://www.exleasingcar.com/ru/auto-details/19521373'); open_lightbox('auto_19521373');">
        <a href="javascript:void(0);">open</a>
    </div>
    <div class="s-car-list auto-block" car-id="19521372"
         onclick="javascript:$('.after_login_redirect').val('https://www.exleasingcar.com/ru/auto-details/19521372'); open_lightbox('auto_19521372');">
        <a href="javascript:void(0);">open</a>
    </div>
    """

    links = extract_vehicle_detail_urls(html, "https://www.exleasingcar.com/ru/auction-209633")

    assert links == {
        "19694139": "https://www.exleasingcar.com/ru/auto-details/19694139",
        "19521373": "https://www.exleasingcar.com/ru/auto-details/19521373",
        "19521372": "https://www.exleasingcar.com/ru/auto-details/19521372",
    }


def test_extract_vehicle_detail_urls_rejects_onclick_with_mismatched_car_id():
    html = """
    <div class="s-car-list" car-id="19694139"
         onclick="$('.after_login_redirect').val('https://www.exleasingcar.com/ru/auto-details/19521373'); open_lightbox('auto_19521373');">
        <a href="javascript:void(0);">open</a>
    </div>
    """

    assert extract_vehicle_detail_urls(html, "https://www.exleasingcar.com") == {}


def test_api_response_recorder_reports_invalid_json():
    from exleasingcar_archive.collector import ApiResponseRecorder

    class FakeResponse:
        url = "https://www.exleasingcar.com/ajax_update_car_form.v3.1.php"
        ok = True
        status = 200

        def body(self):
            return b"not valid JSON"

    recorder = ApiResponseRecorder()
    recorder._on_response(FakeResponse())

    assert recorder.responses == []
    assert len(recorder.errors) == 1
    assert "Could not read JSON" in recorder.errors[0]


def test_manual_reauthentication_pauses_and_retries_same_detail_url(
    monkeypatch,
    tmp_path,
):
    from exleasingcar_archive import collector as collector_module
    from exleasingcar_archive.collector import ExleasingcarCollector

    class FakePage:
        url = "https://www.exleasingcar.com/ru"

        def __init__(self):
            self.goto_calls = []

        def goto(self, url, **kwargs):
            self.goto_calls.append((url, kwargs))
            self.url = url

    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt))
    monkeypatch.setattr(
        collector_module,
        "_wait_for_dynamic_content",
        lambda page: None,
    )

    collector = ExleasingcarCollector(database=None, raw_dir=tmp_path)
    page = FakePage()
    target = "https://www.exleasingcar.com/ru/auto-details/19694139"

    assert collector._pause_for_manual_reauthentication(
        page,
        target,
        "19694139",
    ) is True
    assert page.goto_calls[0][0] == target
    assert len(prompts) == 1

    # Avoid repeated terminal prompts for every remaining card during one run.
    assert collector._pause_for_manual_reauthentication(
        page,
        target,
        "19694139",
    ) is False
    assert len(prompts) == 1


def test_detail_enrichment_persists_fixed_sale_type_and_price(tmp_path, monkeypatch):
    import sqlite3
    from datetime import datetime, timezone

    from exleasingcar_archive.collector import ExleasingcarCollector
    from exleasingcar_archive.database import ArchiveDatabase
    from exleasingcar_archive.ingestion import AuctionContext

    html = """
    <div class="auto-block" car-id="19694139">
        <div class="bids active fix bids_19694139"></div>
        <div class="fixed-price">
            <input class="buy-offer" value="Купить сейчас за € 16 900">
        </div>
    </div>
    """

    class FakePage:
        url = "https://www.exleasingcar.com/ru/auto-details/19694139"

        def __init__(self):
            self.html = html

        def goto(self, url, **kwargs):
            self.url = url

        def content(self):
            return self.html

    monkeypatch.setattr(
        "exleasingcar_archive.collector._wait_for_dynamic_content",
        lambda page: None,
    )
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    collector = ExleasingcarCollector(database, tmp_path / "raw", page_delay_seconds=0)

    result = collector._enrich_vehicle_detail(
        AuctionContext("auction-209633"),
        "19694139",
        "https://www.exleasingcar.com/ru/auto-details/19694139",
        FakePage(),
        datetime(2026, 11, 16, 16, 0, tzinfo=timezone.utc),
        tmp_path / "snapshot",
    )

    assert result == "fixed_price"
    assert collector.sale_types_recognized == 1
    assert collector.lot_price_observations_inserted == 1
    with sqlite3.connect(database.path) as connection:
        row = connection.execute(
            """
            SELECT l.sale_type, p.price_type, p.amount, p.currency
            FROM auction_lots l
            JOIN vehicles v ON v.id = l.vehicle_id
            JOIN lot_price_observations p ON p.auction_lot_id = l.id
            WHERE v.source_car_id = '19694139'
            """
        ).fetchone()

    assert row == ("fixed_price", "fixed_price", "16900", "EUR")

    # Rechecking an unchanged Buy Now amount updates freshness but not price history.
    page = FakePage()
    collector._enrich_vehicle_detail(
        AuctionContext("auction-209633"),
        "19694139",
        "https://www.exleasingcar.com/ru/auto-details/19694139",
        page,
        datetime(2026, 11, 16, 16, 5, tzinfo=timezone.utc),
        tmp_path / "snapshot",
    )
    assert collector.lot_price_observations_inserted == 1
    assert collector.lot_price_observations_unchanged == 1

    # A changed Buy Now amount adds one new observation while preserving the old one.
    page.html = html.replace("16 900", "17 100")
    collector._enrich_vehicle_detail(
        AuctionContext("auction-209633"),
        "19694139",
        "https://www.exleasingcar.com/ru/auto-details/19694139",
        page,
        datetime(2026, 11, 16, 16, 10, tzinfo=timezone.utc),
        tmp_path / "snapshot",
    )
    with sqlite3.connect(database.path) as connection:
        prices = connection.execute(
            """
            SELECT p.amount
            FROM lot_price_observations p
            JOIN auction_lots l ON l.id = p.auction_lot_id
            JOIN vehicles v ON v.id = l.vehicle_id
            WHERE v.source_car_id = '19694139'
            ORDER BY p.id
            """
        ).fetchall()

    assert prices == [("16900",), ("17100",)]
    assert collector.lot_price_observations_inserted == 2
    assert collector.lot_price_observations_unchanged == 1


def test_detail_enrichment_keeps_open_starting_price_out_of_bid_history(
    tmp_path, monkeypatch
):
    import sqlite3
    from datetime import datetime, timezone

    from exleasingcar_archive.collector import ExleasingcarCollector
    from exleasingcar_archive.database import ArchiveDatabase
    from exleasingcar_archive.ingestion import AuctionContext

    html = """
    <div class="auto-block" car-id="car-start">
        <div class="bids active open bids_car-start">
            <div class="current-price">Начальная цена: € 7 500</div>
            <input class="my_bid_car-start" value="0">
        </div>
    </div>
    """

    class FakePage:
        url = "https://www.exleasingcar.com/ru/auto-details/car-start"

        def goto(self, url, **kwargs):
            self.url = url

        def content(self):
            return html

    monkeypatch.setattr(
        "exleasingcar_archive.collector._wait_for_dynamic_content",
        lambda page: None,
    )
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    collector = ExleasingcarCollector(database, tmp_path / "raw", page_delay_seconds=0)

    assert collector._enrich_vehicle_detail(
        AuctionContext("auction-start"),
        "car-start",
        "https://www.exleasingcar.com/ru/auto-details/car-start",
        FakePage(),
        datetime(2026, 11, 16, 16, 0, tzinfo=timezone.utc),
        tmp_path / "snapshot",
    )

    with sqlite3.connect(database.path) as connection:
        displayed = connection.execute(
            "SELECT price_type, amount FROM lot_price_observations"
        ).fetchone()
        bids = connection.execute(
            "SELECT COUNT(*) FROM price_observations"
        ).fetchone()[0]

    assert displayed == ("displayed_auction_price", "7500")
    assert bids == 0


def test_collector_defaults_to_bounded_detail_batch_for_selected_auction():
    from exleasingcar_archive.collector import build_argument_parser

    args = build_argument_parser().parse_args([])
    assert args.max_auctions == 0
    assert args.max_detail_pages == 30


def test_api_response_recorder_retries_body_read_after_request_finished():
    from exleasingcar_archive.collector import ApiResponseRecorder, PlaywrightError

    class FakeContext:
        def __init__(self):
            self.handlers = {}

        def on(self, event, callback):
            self.handlers[event] = callback

    class FakeRequest:
        url = "https://www.exleasingcar.com/ajax_update_car_form.v3.1.php"

        def response(self):
            return response

    class FakeResponse:
        url = "https://www.exleasingcar.com/ajax_update_car_form.v3.1.php"
        ok = True
        status = 200

        def __init__(self, request):
            self.request = request
            self.headers = {"content-type": "application/json"}
            self.body_calls = 0

        def body(self):
            self.body_calls += 1
            if self.body_calls == 1:
                raise PlaywrightError("response body is not ready yet")
            return b'{"car-1": {"highest_offer": "12500"}}'

    request = FakeRequest()
    response = FakeResponse(request)
    context = FakeContext()
    recorder = ApiResponseRecorder()
    recorder.attach(context)

    assert set(context.handlers) == {
        "request", "response", "requestfinished", "requestfailed"
    }

    context.handlers["request"](request)
    assert recorder.pending_requests

    # The early read may fail transiently; it must not be reported as final.
    context.handlers["response"](response)
    assert response.body_calls == 1
    assert recorder.responses == []
    assert recorder.errors == []

    # Retry once the browser confirms that the response body has finished.
    context.handlers["requestfinished"](request)
    assert response.body_calls == 2
    assert recorder.pending_requests == {}
    assert len(recorder.responses) == 1
    assert recorder.responses[0].payload["car-1"]["highest_offer"] == "12500"
    assert recorder.errors == []


def test_api_response_recorder_reports_non_json_http_response():
    from exleasingcar_archive.collector import ApiResponseRecorder

    class FakeContext:
        def __init__(self):
            self.handlers = {}

        def on(self, event, callback):
            self.handlers[event] = callback

    class FakeRequest:
        url = "https://www.exleasingcar.com/ajax_update_car_form.v3.1.php"

        def response(self):
            return response

    class FakeResponse:
        url = "https://www.exleasingcar.com/ajax_update_car_form.v3.1.php"
        ok = True
        status = 200
        request = None

        def __init__(self):
            self.headers = {"content-type": "text/html; charset=UTF-8"}

        def body(self):
            return b"<html><body>session expired</body></html>"

    request = FakeRequest()
    response = FakeResponse()
    response.request = request
    context = FakeContext()
    recorder = ApiResponseRecorder()
    recorder.attach(context)

    context.handlers["request"](request)
    context.handlers["response"](response)
    assert recorder.errors == []
    context.handlers["requestfinished"](request)

    assert recorder.responses == []
    assert len(recorder.errors) == 1
    assert "content-type=text/html" in recorder.errors[0]


def test_api_response_recorder_reports_abort_after_response_headers_once():
    from exleasingcar_archive.collector import ApiResponseRecorder

    class FakeContext:
        def __init__(self):
            self.handlers = {}

        def on(self, event, callback):
            self.handlers[event] = callback

    class FakeRequest:
        url = "https://www.exleasingcar.com/ajax_update_car_form.v3.1.php"

        def response(self):
            return response

        def failure(self):
            return "net::ERR_ABORTED"

    class FakeResponse:
        url = "https://www.exleasingcar.com/ajax_update_car_form.v3.1.php"
        ok = True
        status = 200

        def __init__(self, request):
            self.request = request
            self.headers = {"content-type": "text/html; charset=UTF-8"}

        def body(self):
            return b"<html>session expired</html>"

    request = FakeRequest()
    response = FakeResponse(request)
    context = FakeContext()
    recorder = ApiResponseRecorder()
    recorder.attach(context)

    context.handlers["request"](request)
    context.handlers["response"](response)
    context.handlers["requestfailed"](request)

    assert recorder.pending_requests == {}
    assert recorder.responses == []
    assert len(recorder.errors) == 1
    assert "API request failed before its response body could be captured" in recorder.errors[0]
    assert "net::ERR_ABORTED" in recorder.errors[0]


def test_api_response_recorder_reports_failed_request():
    from exleasingcar_archive.collector import ApiResponseRecorder

    class FakeRequest:
        url = "https://www.exleasingcar.com/ajax_update_car_form.v3.1.php"

        def failure(self):
            return "net::ERR_ABORTED"

    recorder = ApiResponseRecorder()
    request = FakeRequest()
    recorder._on_request(request)
    recorder._on_request_failed(request)

    assert recorder.pending_requests == {}
    assert len(recorder.errors) == 1
    assert "net::ERR_ABORTED" in recorder.errors[0]


def test_api_response_recorder_waits_for_pending_request_completion():
    from exleasingcar_archive.collector import ApiResponseRecorder

    class FakeResponse:
        url = "https://www.exleasingcar.com/ajax_update_car_form.v3.1.php"
        ok = True
        status = 200
        def __init__(self):
            self.headers = {"content-type": "application/json"}

        def body(self):
            return b'{"car-1": {"highest_offer": 12500}}'

    class FakeRequest:
        url = "https://www.exleasingcar.com/ajax_update_car_form.v3.1.php"

        def response(self):
            return FakeResponse()

    class FakePage:
        def __init__(self, callback):
            self.callback = callback
            self.waits = 0

        def wait_for_timeout(self, milliseconds):
            self.waits += 1
            self.callback()

    recorder = ApiResponseRecorder()
    request = FakeRequest()
    recorder._on_request(request)
    page = FakePage(lambda: recorder._on_request_finished(request))

    assert recorder.wait_for_pending(page, timeout_ms=100) is True
    assert page.waits == 1
    assert recorder.pending_requests == {}
    assert len(recorder.responses) == 1


def test_api_response_recorder_reports_request_that_did_not_finish():
    from exleasingcar_archive.collector import ApiResponseRecorder

    class FakeRequest:
        url = "https://www.exleasingcar.com/ajax_update_car_form.v3.1.php"

    class FakePage:
        def wait_for_timeout(self, milliseconds):
            return None

    recorder = ApiResponseRecorder()
    request = FakeRequest()
    recorder._on_request(request)

    assert recorder.wait_for_pending(FakePage(), timeout_ms=0) is False
    assert recorder.errors == []


def test_catalog_detail_coverage_separates_repeated_cards_from_missing_links():
    from exleasingcar_archive.collector import _catalog_detail_coverage

    coverage = _catalog_detail_coverage(
        lots_seen=5,
        catalog_lot_ids={"car-1", "car-2", "car-3", "car-4"},
        detail_url_ids={"car-1", "car-2", "car-3"},
    )

    assert coverage.unique_lot_ids == 4
    assert coverage.repeated_card_occurrences == 1
    assert coverage.detail_urls_found == 3
    assert coverage.lot_ids_without_detail_url == ("car-4",)



def test_catalogue_observation_merge_keeps_newest_and_breaks_timestamp_ties_deterministically():
    from datetime import datetime, timedelta, timezone

    from exleasingcar_archive.collector import _merge_catalog_lot_observation
    from exleasingcar_archive.models import VehicleStatus
    from exleasingcar_archive.parsers import ParsedLot

    first_seen = datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc)
    newer_seen = first_seen + timedelta(seconds=1)
    older = ParsedLot(
        "car-1", "Old title", None, VehicleStatus.ACTIVE, "Active", False, None
    )
    newer = ParsedLot(
        "car-1", "New title", None, VehicleStatus.ACTIVE, "Active", False, None
    )

    observations = {}
    _merge_catalog_lot_observation(observations, newer, newer_seen)
    _merge_catalog_lot_observation(observations, older, first_seen)
    assert observations["car-1"] == (newer, newer_seen)

    active = ParsedLot(
        "car-2", "Car", None, VehicleStatus.ACTIVE, "Active", False, None
    )
    completed = ParsedLot(
        "car-2", "Car", None, VehicleStatus.COMPLETED, "Finished", False, None
    )
    forward = {}
    reverse = {}
    _merge_catalog_lot_observation(forward, active, first_seen)
    _merge_catalog_lot_observation(forward, completed, first_seen)
    _merge_catalog_lot_observation(reverse, completed, first_seen)
    _merge_catalog_lot_observation(reverse, active, first_seen)

    assert forward["car-2"] == reverse["car-2"]
    assert forward["car-2"][0].status is VehicleStatus.COMPLETED


def test_catalogue_terminal_status_is_saved_before_detail_rotation(monkeypatch, tmp_path):
    import sqlite3
    from datetime import datetime, timezone

    from exleasingcar_archive import collector as collector_module
    from exleasingcar_archive.collector import CapturedApiResponse, ExleasingcarCollector
    from exleasingcar_archive.database import ArchiveDatabase
    from exleasingcar_archive.metadata_parser import (
        ParsedAuctionCatalogMetadata,
        ParsedAuctionListing,
        ParsedAuctionLotMetadata,
        ParsedCatalogPagination,
    )
    from exleasingcar_archive.models import AuctionStatus, SaleType, VehicleStatus
    from exleasingcar_archive.parsers import ParsedAuctionPage, ParsedLot

    auction_id = "auction-terminal"
    catalog_url = "https://www.exleasingcar.com/auction-terminal?page=1"
    detail_url = "https://www.exleasingcar.com/ru/auto-details/car-terminal"
    observed_at = datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc)
    database = ArchiveDatabase(tmp_path / "terminal-before-detail.sqlite3")
    database.initialize()
    database.upsert_auction(auction_id)
    database.upsert_vehicle("car-terminal", displayed_title="Volvo V90")
    database.upsert_auction_lot(
        "car-terminal",
        auction_id,
        sale_type=SaleType.OPEN_AUCTION,
        collection_eligible=True,
    )

    listing = ParsedAuctionListing(
        exleasingcar_auction_id=auction_id,
        source_auction_name="Test auction",
        source_auction_country_code="DE",
        source_auction_country_name="Germany",
        auction_type_label="English auction",
        auction_status_label="ACTIVE",
        vehicle_count=1,
        source_url=catalog_url,
        markers=(),
    )
    catalog_lot = ParsedAuctionLotMetadata(
        source_car_id="car-terminal",
        displayed_title="Volvo V90",
        displayed_lot_reference="LOT-1",
        listing_code_attribute="code-1",
        make_label="Volvo",
        mileage_km=100000,
    )
    terminal_lot = ParsedLot(
        source_car_id="car-terminal",
        title="Volvo V90",
        source_url=detail_url,
        status=VehicleStatus.COMPLETED,
        raw_status="Finished",
        is_hot_bid=False,
        current_price=None,
    )

    monkeypatch.setattr(
        collector_module, "_wait_for_dynamic_content", lambda page: None
    )
    monkeypatch.setattr(
        collector_module,
        "parse_auction_catalog_metadata",
        lambda html, url: ParsedAuctionCatalogMetadata(
            auction_id, (catalog_lot,)
        ),
    )
    monkeypatch.setattr(
        collector_module,
        "parse_auction_list",
        lambda html, url: ParsedAuctionPage(
            auction_id, AuctionStatus.ACTIVE, "ACTIVE", (terminal_lot,)
        ),
    )
    monkeypatch.setattr(
        collector_module,
        "parse_catalog_pagination",
        lambda html, url: ParsedCatalogPagination(
            catalog_url, 1, (catalog_url,), None
        ),
    )
    monkeypatch.setattr(
        collector_module,
        "extract_vehicle_detail_urls",
        lambda html, url: {"car-terminal": detail_url},
    )

    class FakePage:
        def __init__(self):
            self.url = ""
            self.goto_calls = []

        def goto(self, url, **kwargs):
            self.goto_calls.append(url)
            self.url = url
            assert url == catalog_url, "completed lot must not be opened for detail"
            collector.recorder.responses.append(
                CapturedApiResponse(
                    url="https://www.exleasingcar.com/ajax_update_car_form.v3.1.php",
                    payload={"car-terminal": {"highest_offer": "1234"}},
                    observed_at=observed_at,
                )
            )

        def content(self):
            return "<html><body>catalogue</body></html>"

    collector = ExleasingcarCollector(
        database, tmp_path / "raw-terminal", page_delay_seconds=0
    )
    monkeypatch.setattr(collector.recorder, "wait_for_pending", lambda page: True)
    page = FakePage()

    collector._collect_auction(
        listing,
        page,
        tmp_path / "snapshots-terminal",
        max_detail_pages=1,
    )

    assert page.goto_calls == [catalog_url]
    assert database.get_completed_open_auction_lots(
        auction_id, {"car-terminal"}
    ) == {"car-terminal"}
    with sqlite3.connect(database.path) as connection:
        stored_price = connection.execute(
            """
            SELECT po.highest_offer
            FROM price_observations po
            JOIN auction_lots al ON al.id = po.auction_lot_id
            JOIN vehicles v ON v.id = al.vehicle_id
            WHERE v.source_car_id = 'car-terminal'
            """
        ).fetchone()
        detail_attempted = connection.execute(
            """
            SELECT al.last_detail_attempted_at
            FROM auction_lots al
            JOIN vehicles v ON v.id = al.vehicle_id
            WHERE v.source_car_id = 'car-terminal'
            """
        ).fetchone()[0]

    assert stored_price == ("1234",)
    assert detail_attempted is None


def test_dismiss_notification_prompt_chooses_no_thanks_and_waits_for_it_to_close():
    from exleasingcar_archive.collector import ExleasingcarCollector

    class FakeLocator:
        def __init__(self, *, visible=True, on_click=None):
            self.visible = visible
            self.on_click = on_click
            self.role_button = None
            self.fallback_button = None
            self.text_button = None
            self.clicked = 0

        def count(self):
            return int(self.visible)

        def nth(self, index):
            return self

        def is_visible(self):
            return self.visible

        def get_by_role(self, role, *, name):
            assert role == "button"
            assert name.search("NO THANKS")
            return self.role_button

        def locator(self, selector):
            assert "cancel" in selector
            return self.fallback_button

        def get_by_text(self, pattern):
            assert pattern.search("NO THANKS")
            return self.text_button

        def click(self, *, timeout):
            self.clicked += 1
            if self.on_click:
                self.on_click()

        def wait_for(self, *, state, timeout):
            assert state == "hidden"
            assert self.visible is False

    class FakePage:
        def __init__(self, container):
            self.container = container

        def locator(self, selector):
            assert selector == "#onesignal-slidedown-container"
            return self.container

    container = FakeLocator()
    no_thanks = FakeLocator(on_click=lambda: setattr(container, "visible", False))
    container.role_button = no_thanks
    container.fallback_button = FakeLocator(visible=False)
    container.text_button = FakeLocator(visible=False)

    assert ExleasingcarCollector._dismiss_notification_prompt(
        FakePage(container)
    ) is True
    assert no_thanks.clicked == 1
    assert container.visible is False


def test_dismiss_notification_prompt_does_nothing_when_prompt_is_absent():
    from exleasingcar_archive.collector import ExleasingcarCollector

    class FakeLocator:
        def count(self):
            return 0

    class FakePage:
        def locator(self, selector):
            assert selector == "#onesignal-slidedown-container"
            return FakeLocator()

    assert ExleasingcarCollector._dismiss_notification_prompt(FakePage()) is False


def test_dismiss_notification_prompt_never_grants_permission():
    from exleasingcar_archive.collector import ExleasingcarCollector

    class FakeLocator:
        def __init__(self, *, visible=True):
            self.visible = visible
            self.clicked = 0
            self.role_queries = []

        def count(self):
            return int(self.visible)

        def nth(self, index):
            return self

        def is_visible(self):
            return self.visible

        def get_by_role(self, role, *, name):
            self.role_queries.append(name.pattern)
            return FakeLocator(visible=False)

        def locator(self, selector):
            return FakeLocator(visible=False)

        def get_by_text(self, pattern):
            return FakeLocator(visible=False)

    class FakePage:
        def __init__(self, container):
            self.container = container

        def locator(self, selector):
            return self.container

    container = FakeLocator()

    with pytest.raises(RuntimeError, match="NO THANKS"):
        ExleasingcarCollector._dismiss_notification_prompt(FakePage(container))

    assert container.clicked == 0
    assert all("ALLOW" not in pattern for pattern in container.role_queries)


def test_catalogue_scan_resumes_api_and_detail_data_after_a_run_budget_pause(
    monkeypatch,
    tmp_path,
):
    import sqlite3
    from datetime import datetime, timedelta, timezone

    from exleasingcar_archive import collector as collector_module
    from exleasingcar_archive.collector import CapturedApiResponse
    from exleasingcar_archive.database import ArchiveDatabase
    from exleasingcar_archive.metadata_parser import (
        ParsedAuctionCatalogMetadata,
        ParsedAuctionListing,
        ParsedAuctionLotMetadata,
        ParsedCatalogPagination,
    )
    from exleasingcar_archive.models import AuctionStatus, SaleType, VehicleStatus
    from exleasingcar_archive.parsers import ParsedAuctionPage, ParsedLot
    from exleasingcar_archive.scheduler import RunBudget, RunLimits

    first_url = "https://www.exleasingcar.com/auction-210547?page=1"
    second_url = "https://www.exleasingcar.com/auction-210547?page=2"
    observed_at = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    database = ArchiveDatabase(tmp_path / "resumable-collector.sqlite3")
    database.initialize()

    listing = ParsedAuctionListing(
        exleasingcar_auction_id="210547",
        source_auction_name="BCA DE",
        source_auction_country_code="DE",
        source_auction_country_name="Germany",
        auction_type_label="English auction",
        auction_status_label="ACTIVE",
        vehicle_count=1,
        source_url=first_url,
        markers=(),
    )

    first_catalog_lot = ParsedAuctionLotMetadata(
        source_car_id="car-1",
        displayed_title="Ford Focus",
        displayed_lot_reference="LOT-1",
        listing_code_attribute="code-1",
        make_label="Ford",
        mileage_km=12000,
    )
    first_status_lot = ParsedLot(
        source_car_id="car-1",
        title="Ford Focus",
        source_url="https://www.exleasingcar.com/ru/auto-details/car-1",
        status=VehicleStatus.ACTIVE,
        raw_status="Active",
        is_hot_bid=False,
        current_price=None,
    )

    def parse_catalog(html, page_url):
        if page_url == first_url:
            return ParsedAuctionCatalogMetadata("210547", (first_catalog_lot,))
        return ParsedAuctionCatalogMetadata("210547", ())

    def parse_status(html, page_url):
        lots = (first_status_lot,) if page_url == first_url else ()
        return ParsedAuctionPage(
            source_auction_id="210547",
            status=AuctionStatus.ACTIVE,
            raw_status="ACTIVE",
            lots=lots,
        )

    def parse_pagination(html, page_url):
        return ParsedCatalogPagination(
            current_page_url=page_url,
            current_page_number=1 if page_url == first_url else 2,
            page_urls=(first_url, second_url),
            next_page_url=second_url if page_url == first_url else None,
        )

    monkeypatch.setattr(
        collector_module,
        "_wait_for_dynamic_content",
        lambda page: None,
    )
    monkeypatch.setattr(
        collector_module,
        "parse_auction_catalog_metadata",
        parse_catalog,
    )
    monkeypatch.setattr(collector_module, "parse_auction_list", parse_status)
    monkeypatch.setattr(
        collector_module,
        "parse_catalog_pagination",
        parse_pagination,
    )
    monkeypatch.setattr(
        collector_module,
        "extract_vehicle_detail_urls",
        lambda html, url: (
            {"car-1": "https://www.exleasingcar.com/ru/auto-details/car-1"}
            if url == first_url
            else {}
        ),
    )

    class FakePage:
        def __init__(self, capture_api=False):
            self.url = ""
            self.capture_api = capture_api
            self.goto_calls = []

        def goto(self, url, **kwargs):
            self.goto_calls.append(url)
            self.url = url
            if self.capture_api and url == first_url:
                first_collector.recorder.responses.append(
                    CapturedApiResponse(
                        url=(
                            "https://www.exleasingcar.com/"
                            "ajax_update_car_form.v3.1.php"
                        ),
                        payload={"car-1": {"highest_offer": "10000"}},
                        observed_at=observed_at,
                    )
                )

        def content(self):
            return "<html><body>synthetic catalogue</body></html>"

    first_collector = ExleasingcarCollector(
        database,
        tmp_path / "raw",
        page_delay_seconds=0,
    )
    first_page = FakePage(capture_api=True)
    monkeypatch.setattr(
        first_collector.recorder,
        "wait_for_pending",
        lambda page: True,
    )
    first_budget = RunBudget(
        RunLimits(
            max_duration_seconds=60,
            max_catalog_pages=1,
            max_live_lots=1,
            max_detail_pages=0,
        )
    )

    first_collector._collect_auction(
        listing,
        first_page,
        tmp_path / "snapshots-first",
        max_detail_pages=0,
        run_budget=first_budget,
    )

    assert first_page.goto_calls == [first_url]
    progress = database.get_catalog_scan_progress(1)
    assert progress is not None
    assert progress.status == "in_progress"
    assert progress.next_page_url == second_url
    assert progress.next_page_number == 2

    assert database.get_pending_catalog_api_pages("210547") == ()
    with sqlite3.connect(database.path) as connection:
        deferred_count = connection.execute(
            """
            SELECT COUNT(*) FROM catalog_deferred_api_lots
            WHERE processed_at IS NULL
            """
        ).fetchone()[0]
    assert deferred_count == 1

    second_collector = ExleasingcarCollector(
        database,
        tmp_path / "raw",
        page_delay_seconds=0,
    )
    second_page = FakePage()
    monkeypatch.setattr(
        second_collector.recorder,
        "wait_for_pending",
        lambda page: True,
    )

    def enrich_open_lot(
        auction,
        car_id,
        detail_url,
        page,
        detail_observed_at,
        auction_dir,
    ):
        database.set_auction_lot_qualification(
            car_id,
            "210547",
            sale_type=SaleType.OPEN_AUCTION,
            has_fixed_price=False,
            collection_eligible=True,
            checked_at=detail_observed_at,
        )
        second_collector.recorder.responses.append(
            CapturedApiResponse(
                url="https://www.exleasingcar.com/ajax_update_car_form.v3.1.php",
                payload={
                    "car-1": {
                        "highest_offer": "11000",
                        "user_offer": "123456",
                        "actual_car_price": "654321",
                    }
                },
                observed_at=observed_at + timedelta(minutes=1),
            )
        )
        return "open_auction"

    monkeypatch.setattr(
        second_collector,
        "_enrich_vehicle_detail",
        enrich_open_lot,
    )
    second_budget = RunBudget(
        RunLimits(
            max_duration_seconds=60,
            max_catalog_pages=1,
            max_live_lots=2,
            max_detail_pages=1,
        )
    )

    second_collector._collect_auction(
        listing,
        second_page,
        tmp_path / "snapshots-second",
        max_detail_pages=1,
        run_budget=second_budget,
    )

    assert second_page.goto_calls == [second_url]
    completed = database.get_catalog_scan_progress(1)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.pages_completed == 2
    assert database.get_pending_catalog_api_pages("210547") == ()


    with sqlite3.connect(database.path) as connection:
        stored_prices = connection.execute(
            """
            SELECT highest_offer
            FROM price_observations
            JOIN auction_lots ON auction_lots.id = price_observations.auction_lot_id
            JOIN vehicles ON vehicles.id = auction_lots.vehicle_id
            WHERE vehicles.source_car_id = 'car-1'
            ORDER BY price_observations.observed_at, price_observations.id
            """
        ).fetchall()
        stored_detail_check = connection.execute(
            """
            SELECT last_detail_checked_at
            FROM auction_lots
            JOIN vehicles ON vehicles.id = auction_lots.vehicle_id
            WHERE vehicles.source_car_id = 'car-1'
            """
        ).fetchone()[0]

    assert stored_prices == [("10000",), ("11000",)]
    assert stored_detail_check is not None
    assert database.get_pending_catalog_api_lot_updates("210547") == ()

    saved_api_responses = database.get_catalog_scan_pages(completed.scan_id)[0].api_responses
    assert len(saved_api_responses) == 2
    assert all(
        "user_offer" not in response["payload"]["car-1"]
        and "actual_car_price" not in response["payload"]["car-1"]
        for response in saved_api_responses
    )


def test_collector_cli_defaults_to_global_queue_mode_and_supports_planning():
    args = build_argument_parser().parse_args([])

    assert args.max_auctions == 0
    assert args.force_auction_discovery is False
    assert args.plan_only is False

    plan = build_argument_parser().parse_args(["--plan-only", "--force-auction-discovery"])
    assert plan.plan_only is True
    assert plan.force_auction_discovery is True


def test_collector_cli_exposes_safe_run_wide_limits():
    args = build_argument_parser().parse_args([])

    assert args.max_run_seconds == 900
    assert args.max_catalog_pages_per_run == 40
    assert args.max_live_lots_per_run == 500
    assert args.max_detail_pages_per_run == 60


def test_auction_listing_round_trips_from_persistent_queue():
    from dataclasses import asdict

    from exleasingcar_archive.collector import _auction_listing_from_queue
    from exleasingcar_archive.metadata_parser import ParsedAuctionListing
    from exleasingcar_archive.models import AuctionCardMarker

    original = ParsedAuctionListing(
        exleasingcar_auction_id="auction-queue",
        source_auction_name="Test fleet",
        source_auction_country_code="DE",
        source_auction_country_name="Germany",
        auction_type_label="English auction",
        auction_status_label="FINAL CALL",
        vehicle_count=18,
        source_url="https://www.exleasingcar.com/ru/авто-аукционы/аукцион-queue",
        markers=(
            AuctionCardMarker("ac.svg", "auction_format", "English auction"),
        ),
    )

    restored = _auction_listing_from_queue(asdict(original))

    assert restored == original


def test_global_auction_discovery_checkpoints_and_resumes_all_pages(
    monkeypatch, tmp_path
):
    from pathlib import Path

    from exleasingcar_archive import collector as collector_module
    from exleasingcar_archive.collector import ExleasingcarCollector
    from exleasingcar_archive.database import ArchiveDatabase
    from exleasingcar_archive.scheduler import RunBudget, RunLimits

    fixture_path = Path(__file__).parent / "fixtures" / "auction_listings.html"
    first_html = fixture_path.read_text(encoding="utf-8")
    second_html = first_html.replace("210547", "210548").replace(
        "BCA DE 3.3", "BCA DE 3.4"
    )
    first_url = "https://www.exleasingcar.com/ru/авто-аукционы"
    second_url = "https://www.exleasingcar.com/ru/авто-аукционы?page=2"

    def with_pagination(html, current, *, has_next):
        next_link = (
            f'<a class="pagination-next" href="{second_url}">Next</a>'
            if has_next
            else ""
        )
        return html + f"""
        <nav><ul class="pagination">
          <li><a class="pagination-page {'current' if current == 1 else ''}" href="{first_url}">1</a></li>
          <li><a class="pagination-page {'current' if current == 2 else ''}" href="{second_url}">2</a></li>
          {next_link}
        </ul></nav>
        """

    pages = {
        first_url: with_pagination(first_html, 1, has_next=True),
        second_url: with_pagination(second_html, 2, has_next=False),
    }

    class FakePage:
        def __init__(self):
            self.url = first_url
            self.goto_calls = []

        def goto(self, url, **kwargs):
            self.goto_calls.append(url)
            self.url = url

        def content(self):
            return pages[self.url]

    database = ArchiveDatabase(tmp_path / "global-discovery.sqlite3")
    database.initialize()
    collector = ExleasingcarCollector(
        database,
        tmp_path / "raw",
        page_delay_seconds=0,
    )
    monkeypatch.setattr(collector.recorder, "wait_for_pending", lambda page: True)
    monkeypatch.setattr(collector, "_is_logged_in", lambda page: True)
    monkeypatch.setattr(
        collector_module, "_wait_for_dynamic_content", lambda page: None
    )

    page = FakePage()
    first_budget = RunBudget(
        RunLimits(
            max_duration_seconds=60,
            max_catalog_pages=1,
            max_live_lots=100,
            max_detail_pages=0,
        )
    )
    first_discovered, first_processed = collector._discover_auction_listings(
        page,
        auction_list_url=first_url,
        snapshot_dir=tmp_path / "snapshots",
        initial_page_html=pages[first_url],
        initial_page_url=first_url,
        run_budget=first_budget,
    )

    assert (first_discovered, first_processed) == (1, 1)
    assert page.goto_calls == []
    progress = database.start_or_resume_auction_discovery(first_url)
    assert progress.status == "in_progress"
    assert progress.next_page_url == second_url
    assert database.get_auction_bootstrap_queue_summary()["pending"] == 1

    second_budget = RunBudget(
        RunLimits(
            max_duration_seconds=60,
            max_catalog_pages=2,
            max_live_lots=100,
            max_detail_pages=0,
        )
    )
    second_discovered, second_processed = collector._discover_auction_listings(
        page,
        auction_list_url=first_url,
        snapshot_dir=tmp_path / "snapshots-resume",
        initial_page_html=pages[first_url],
        initial_page_url=first_url,
        run_budget=second_budget,
    )

    assert (second_discovered, second_processed) == (1, 1)
    assert page.goto_calls == [second_url]
    completed = database.start_or_resume_auction_discovery(first_url)
    assert completed.status == "completed"
    assert completed.pages_completed == 2
    assert database.get_auction_bootstrap_queue_summary()["pending"] == 2


def test_bootstrap_queue_waits_for_actionable_api_backlogs_to_drain(tmp_path):
    from datetime import datetime, timezone

    from exleasingcar_archive.collector import _auction_bootstrap_is_drained
    from exleasingcar_archive.database import ArchiveDatabase
    from exleasingcar_archive.models import SaleType

    database = ArchiveDatabase(tmp_path / "bootstrap-api-backlog.sqlite3")
    database.initialize()
    auction_id = "auction-api-backlog"
    car_id = "car-api-backlog"
    page_url = "https://www.exleasingcar.com/auction-api-backlog?page=1"
    api_url = "https://www.exleasingcar.com/ajax_update_car_form.v3.1.php"
    observed_at = datetime(2026, 11, 12, 12, 5, tzinfo=timezone.utc)

    database.upsert_auction(auction_id, observed_at=observed_at)
    database.upsert_vehicle(car_id, observed_at=observed_at)
    database.upsert_auction_lot(
        car_id,
        auction_id,
        sale_type=SaleType.OPEN_AUCTION,
        collection_eligible=True,
        observed_at=observed_at,
    )
    scan = database.start_or_resume_catalog_scan(
        auction_id, "bootstrap", page_url, observed_at=observed_at
    )
    assert database.record_catalog_page_completed(
        scan.scan_id,
        1,
        page_url,
        next_page_url=None,
        next_page_number=None,
        item_count=1,
        page_data={},
        api_responses=(
            {
                "url": api_url,
                "payload": {car_id: {"highest_offer": "12000"}},
                "observed_at": observed_at,
            },
        ),
        observed_at=observed_at,
    )
    assert database.complete_catalog_scan(
        scan.scan_id, observed_at=observed_at
    )

    assert not _auction_bootstrap_is_drained(database, auction_id)
    pending_page = database.get_pending_catalog_api_pages(auction_id)[0]
    assert database.advance_catalog_api_response_cursor(
        pending_page.page_id, 0
    )
    assert _auction_bootstrap_is_drained(database, auction_id)

    assert database.defer_catalog_api_lot_updates(
        pending_page.page_id,
        {
            "url": api_url,
            "payload": {car_id: {"highest_offer": "12500"}},
            "observed_at": observed_at,
        },
        {car_id},
    ) == 1
    pending_updates = database.get_pending_catalog_api_lot_updates(auction_id)
    assert len(pending_updates) == 1
    assert not _auction_bootstrap_is_drained(database, auction_id)
    assert database.mark_catalog_api_lot_update_processed(
        pending_updates[0]["id"], observed_at=observed_at
    )
    assert _auction_bootstrap_is_drained(database, auction_id)


def test_unknown_sale_mode_does_not_block_public_updates_for_known_lots(tmp_path):
    import sqlite3
    from datetime import datetime, timezone

    from exleasingcar_archive.collector import ExleasingcarCollector
    from exleasingcar_archive.database import ArchiveDatabase
    from exleasingcar_archive.ingestion import AuctionContext, LotMetadata
    from exleasingcar_archive.models import SaleType

    database = ArchiveDatabase(tmp_path / "unknown-mode-queue.sqlite3")
    database.initialize()
    auction_id = "auction-known-and-unknown"
    page_url = "https://www.exleasingcar.com/auction-known-and-unknown?page=1"
    api_url = "https://www.exleasingcar.com/ajax_update_car_form.v3.1.php"
    observed_at = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)

    database.upsert_auction(auction_id, observed_at=observed_at)
    database.upsert_vehicle("car-unknown", observed_at=observed_at)
    database.upsert_auction_lot(
        "car-unknown",
        auction_id,
        sale_type=None,
        collection_eligible=False,
        observed_at=observed_at,
    )
    database.upsert_vehicle("car-open", observed_at=observed_at)
    database.upsert_auction_lot(
        "car-open",
        auction_id,
        sale_type=SaleType.OPEN_AUCTION,
        collection_eligible=True,
        observed_at=observed_at,
    )

    scan = database.start_or_resume_catalog_scan(
        auction_id,
        "bootstrap",
        page_url,
        observed_at=observed_at,
    )
    assert database.record_catalog_page_completed(
        scan.scan_id,
        1,
        page_url,
        next_page_url=None,
        next_page_number=None,
        item_count=2,
        page_data={},
        api_responses=(
            {
                "url": api_url,
                "payload": {
                    "car-unknown": {"highest_offer": "8000"},
                    "car-open": {"highest_offer": "10000"},
                },
                "observed_at": observed_at,
            },
        ),
        observed_at=observed_at,
    )

    collector = ExleasingcarCollector(
        database,
        tmp_path / "raw",
        page_delay_seconds=0,
    )
    eligible_ids = {"car-open"}
    collector._persist_api_updates(
        AuctionContext(auction_id),
        eligible_ids,
        {
            "car-unknown": LotMetadata(),
            "car-open": LotMetadata(),
        },
        open_auction_car_ids={"car-open"},
    )

    with sqlite3.connect(database.path) as connection:
        prices = connection.execute(
            """
            SELECT vehicles.source_car_id, price_observations.highest_offer
            FROM price_observations
            JOIN auction_lots
              ON auction_lots.id = price_observations.auction_lot_id
            JOIN vehicles ON vehicles.id = auction_lots.vehicle_id
            ORDER BY vehicles.source_car_id
            """
        ).fetchall()
        unknown_lot = connection.execute(
            """
            SELECT sale_type, collection_eligible
            FROM auction_lots
            JOIN vehicles ON vehicles.id = auction_lots.vehicle_id
            WHERE vehicles.source_car_id = 'car-unknown'
            """
        ).fetchone()

    assert prices == [("car-open", "10000")]
    assert unknown_lot == (None, 0)
    assert eligible_ids == {"car-open"}
    assert database.get_pending_catalog_api_pages(auction_id) == ()
    assert database.get_pending_catalog_api_lot_updates(auction_id) == ()

    # The public snapshot remains available for audit, but its unclassified
    # lot was not used to qualify or price the archive record.
    saved_page = database.get_catalog_scan_pages(scan.scan_id)[0]
    assert saved_page.api_responses_processed == 1
    assert saved_page.api_responses[0]["payload"]["car-unknown"] == {
        "highest_offer": "8000"
    }

    # Once detail enrichment establishes the auction mode, the isolated public
    # payload is replayed without needing to refetch the original catalogue page.
    database.set_auction_lot_qualification(
        "car-unknown",
        auction_id,
        sale_type=SaleType.OPEN_AUCTION,
        has_fixed_price=False,
        collection_eligible=False,
        checked_at=observed_at,
    )
    collector._persist_api_updates(
        AuctionContext(auction_id),
        eligible_ids,
        {
            "car-unknown": LotMetadata(),
            "car-open": LotMetadata(),
        },
        open_auction_car_ids={"car-open"},
        open_auction_candidate_ids={"car-unknown"},
    )

    with sqlite3.connect(database.path) as connection:
        updated_prices = connection.execute(
            """
            SELECT vehicles.source_car_id, price_observations.highest_offer
            FROM price_observations
            JOIN auction_lots
              ON auction_lots.id = price_observations.auction_lot_id
            JOIN vehicles ON vehicles.id = auction_lots.vehicle_id
            ORDER BY vehicles.source_car_id
            """
        ).fetchall()
        updated_unknown_lot = connection.execute(
            """
            SELECT sale_type, collection_eligible
            FROM auction_lots
            JOIN vehicles ON vehicles.id = auction_lots.vehicle_id
            WHERE vehicles.source_car_id = 'car-unknown'
            """
        ).fetchone()

    assert updated_prices == [("car-open", "10000"), ("car-unknown", "8000")]
    assert updated_unknown_lot == (SaleType.OPEN_AUCTION.value, 1)
    assert "car-unknown" in eligible_ids
    assert database.get_pending_catalog_api_lot_updates(auction_id) == ()


def test_malformed_deferred_api_update_is_closed_without_losing_raw_snapshot(
    monkeypatch,
    tmp_path,
):
    import sqlite3
    from datetime import datetime, timezone

    from exleasingcar_archive import collector as collector_module
    from exleasingcar_archive.collector import ExleasingcarCollector
    from exleasingcar_archive.database import ArchiveDatabase
    from exleasingcar_archive.ingestion import AuctionContext, LotMetadata
    from exleasingcar_archive.models import SaleType

    database = ArchiveDatabase(tmp_path / "malformed-deferred-api.sqlite3")
    database.initialize()
    auction_id = "auction-malformed-deferred"
    observed_at = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    database.upsert_auction(auction_id, observed_at=observed_at)
    database.upsert_vehicle("car-deferred", observed_at=observed_at)
    database.upsert_auction_lot(
        "car-deferred",
        auction_id,
        sale_type=SaleType.OPEN_AUCTION,
        collection_eligible=True,
        observed_at=observed_at,
    )
    scan = database.start_or_resume_catalog_scan(
        auction_id,
        "bootstrap",
        "https://www.exleasingcar.com/auction-malformed-deferred?page=1",
        observed_at=observed_at,
    )
    assert database.record_catalog_page_completed(
        scan.scan_id,
        1,
        "https://www.exleasingcar.com/auction-malformed-deferred?page=1",
        next_page_url=None,
        next_page_number=None,
        item_count=1,
        page_data={},
        observed_at=observed_at,
    )
    page = database.get_catalog_scan_pages(scan.scan_id)[0]
    assert database.defer_catalog_api_lot_updates(
        page.page_id,
        {
            "url": "https://www.exleasingcar.com/ajax_update_car_form.v3.1.php",
            "payload": {"car-deferred": {"highest_offer": "not-a-number"}},
            "observed_at": observed_at,
        },
        {"car-deferred"},
    ) == 1

    def reject_payload(payload):
        raise ValueError("synthetic invalid API payload")

    monkeypatch.setattr(
        collector_module,
        "parse_car_update_response",
        reject_payload,
    )
    collector = ExleasingcarCollector(
        database,
        tmp_path / "raw",
        page_delay_seconds=0,
    )
    collector._persist_api_updates(
        AuctionContext(auction_id),
        {"car-deferred"},
        {"car-deferred": LotMetadata()},
        open_auction_car_ids={"car-deferred"},
    )

    assert database.get_pending_catalog_api_lot_updates(auction_id) == ()
    with sqlite3.connect(database.path) as connection:
        retained = connection.execute(
            """
            SELECT response_payload_json, processed_at
            FROM catalog_deferred_api_lots
            """
        ).fetchone()
    assert retained is not None
    assert '"not-a-number"' in retained[0]
    assert retained[1] is not None
