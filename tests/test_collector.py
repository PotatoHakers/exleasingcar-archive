from exleasingcar_archive.collector import (
    extract_vehicle_detail_urls,
    filter_updates_for_catalog,
)


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

        def json(self):
            return {"car-1": {"highest_offer": 123}}

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
