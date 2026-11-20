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


def test_save_html_snapshots_remain_enabled_by_default(tmp_path):
    collector = ExleasingcarCollector(database=None, raw_dir=tmp_path)
    snapshot = tmp_path / "auction" / "vehicle.html"

    collector._save_html(snapshot, "<p>public vehicle content</p>")

    assert snapshot.read_text(encoding="utf-8") == "<p>public vehicle content</p>"


def test_collector_cli_can_disable_html_snapshots():
    args = build_argument_parser().parse_args(["--no-html-snapshots"])

    assert args.no_html_snapshots is True


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

        def finished(self):
            return None

        def json(self):
            raise ValueError("response body is not JSON")

    recorder = ApiResponseRecorder()
    recorder._on_response(FakeResponse())

    assert recorder.responses == []
    assert len(recorder.errors) == 1
    assert "response body is not JSON" in recorder.errors[0]


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

    result = collector._enrich_vehicle_detail(
        AuctionContext("auction-209633"),
        "19694139",
        "https://www.exleasingcar.com/ru/auto-details/19694139",
        FakePage(),
        datetime(2026, 11, 16, 16, 0, tzinfo=timezone.utc),
        tmp_path / "snapshot",
    )

    assert result is True
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
    assert args.max_auctions == 1
    assert args.max_detail_pages == 30


def test_api_response_recorder_captures_body_on_response_before_navigation():
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

        def __init__(self, request):
            self.request = request
            self.headers = {"content-type": "application/json"}

        def body(self):
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

    # The response body is consumed before requestfinished / next navigation.
    context.handlers["response"](response)
    assert len(recorder.responses) == 1
    assert recorder.responses[0].payload["car-1"]["highest_offer"] == "12500"
    assert recorder.errors == []

    context.handlers["requestfinished"](request)
    assert recorder.pending_requests == {}
    assert len(recorder.responses) == 1


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
        headers = {"content-type": "text/html; charset=UTF-8"}

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
    context.handlers["requestfinished"](request)

    assert recorder.responses == []
    assert len(recorder.errors) == 1
    assert "content-type=text/html" in recorder.errors[0]


def test_api_response_recorder_does_not_duplicate_abort_after_response_error():
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
        headers = {"content-type": "text/html; charset=UTF-8"}

        def __init__(self, request):
            self.request = request

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
    assert "content-type=text/html" in recorder.errors[0]


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
    assert len(recorder.errors) == 1
    assert "Timed out waiting for 1 API request" in recorder.errors[0]


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
