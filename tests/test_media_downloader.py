from __future__ import annotations

import hashlib
import sqlite3

import httpx

from exleasingcar_archive.database import ArchiveDatabase
from exleasingcar_archive.media_downloader import (
    MediaDownloader,
    MediaDownloaderSettings,
)
from exleasingcar_archive.r2_storage import CloudflareR2Storage, R2Settings


class FakeR2Client:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.put_calls: list[str] = []
        self.fail_put_attempts = 0

    def head_object(self, **kwargs):
        if kwargs["Key"] not in self.objects:
            error = RuntimeError("missing")
            error.response = {
                "Error": {"Code": "404"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            }
            raise error
        return {}

    def put_object(self, **kwargs):
        if self.fail_put_attempts:
            self.fail_put_attempts -= 1
            raise RuntimeError("temporary R2 failure")
        body = kwargs["Body"]
        self.objects[kwargs["Key"]] = body if isinstance(body, bytes) else body.read()
        self.put_calls.append(kwargs["Key"])


def _database(tmp_path, sources):
    database = ArchiveDatabase(tmp_path / "archive.sqlite3")
    database.initialize()
    database.upsert_auction("209633")
    database.upsert_vehicle("19702473")
    database.upsert_auction_lot("19702473", "209633")
    for media_type, url in sources:
        database.upsert_lot_media_source(
            "19702473",
            "209633",
            media_type,
            url,
        )
    return database


def _storage(client=None):
    r2_client = client or FakeR2Client()
    storage = CloudflareR2Storage(
        R2Settings(
            account_id="a" * 32,
            bucket_name="private-archive",
            access_key_id="test-access",
            secret_access_key="test-secret",
        ),
        client=r2_client,
    )
    return storage, r2_client


def _http_client(handler):
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        timeout=5,
    )


def _photo_response(body=b"\xff\xd8\xffsynthetic-jpeg"):
    return httpx.Response(
        200,
        headers={"content-type": "image/jpeg"},
        content=body,
    )


def test_batch_size_limits_the_number_of_sources_processed(tmp_path):
    sources = [
        ("photo", "https://data.exleasingcar.com/one.jpg"),
        ("report", "https://data.exleasingcar.com/two.pdf"),
    ]
    database = _database(tmp_path, sources)

    def handler(request):
        if request.url.path.endswith(".pdf"):
            return httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=b"%PDF-1.7 synthetic report",
            )
        return _photo_response()

    storage, r2_client = _storage()
    client = _http_client(handler)
    with MediaDownloader(
        database,
        storage,
        settings=MediaDownloaderSettings(batch_size=1, backoff_seconds=0),
        client=client,
        sleep=lambda _: None,
    ) as downloader:
        summary = downloader.download_batch()

    assert summary.selected == 1
    assert summary.downloaded == 1
    assert summary.uploaded == 1
    assert summary.failed == 0
    assert len(r2_client.put_calls) == 1
    assert len(database.get_pending_media_sources(10)) == 1
    client.close()


def test_redirect_to_untrusted_host_is_rejected_before_following_it(tmp_path):
    database = _database(
        tmp_path,
        [("photo", "https://data.exleasingcar.com/redirect.jpg")],
    )
    requested_hosts = []

    def handler(request):
        requested_hosts.append(request.url.host)
        return httpx.Response(
            302,
            headers={"location": "https://evil.example/payload.jpg"},
        )

    storage, r2_client = _storage()
    client = _http_client(handler)
    with MediaDownloader(
        database,
        storage,
        client=client,
        sleep=lambda _: None,
    ) as downloader:
        summary = downloader.download_batch()

    assert summary.failed == 1
    assert requested_hosts == ["data.exleasingcar.com"]
    assert r2_client.put_calls == []
    row = database.get_lot_media_sources("19702473", "209633")[0]
    assert row["download_status"] == "failed"
    assert "evil.example" not in str(row["last_error"])
    client.close()


def test_file_signature_must_match_content_type_and_failure_is_isolated(tmp_path):
    database = _database(
        tmp_path,
        [
            ("photo", "https://data.exleasingcar.com/wrong-type.jpg"),
            ("photo", "https://data.exleasingcar.com/valid.jpg"),
        ],
    )

    def handler(request):
        if request.url.path.endswith("wrong-type.jpg"):
            return httpx.Response(
                200,
                headers={"content-type": "image/jpeg"},
                content=b"\x89PNG\r\n\x1a\nnot-a-jpeg",
            )
        return _photo_response()

    storage, r2_client = _storage()
    client = _http_client(handler)
    with MediaDownloader(
        database,
        storage,
        settings=MediaDownloaderSettings(batch_size=10, backoff_seconds=0),
        client=client,
        sleep=lambda _: None,
    ) as downloader:
        summary = downloader.download_batch()

    assert summary.selected == 2
    assert summary.downloaded == 1
    assert summary.failed == 1
    assert len(r2_client.put_calls) == 1
    statuses = [
        row["download_status"]
        for row in database.get_lot_media_sources("19702473", "209633")
    ]
    assert sorted(statuses) == ["downloaded", "failed"]
    client.close()


def test_streamed_body_size_is_limited_without_content_length(tmp_path):
    database = _database(
        tmp_path,
        [("photo", "https://data.exleasingcar.com/large.jpg")],
    )

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg"},
            content=b"\xff\xd8\xff" + b"x" * 32,
        )

    storage, r2_client = _storage()
    client = _http_client(handler)
    with MediaDownloader(
        database,
        storage,
        settings=MediaDownloaderSettings(
            max_photo_bytes=10,
            backoff_seconds=0,
        ),
        client=client,
        sleep=lambda _: None,
    ) as downloader:
        summary = downloader.download_batch()

    assert summary.failed == 1
    assert r2_client.put_calls == []
    client.close()


def test_transient_http_status_is_retried(tmp_path):
    database = _database(
        tmp_path,
        [("photo", "https://data.exleasingcar.com/retry.jpg")],
    )
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503)
        return _photo_response()

    storage, r2_client = _storage()
    client = _http_client(handler)
    with MediaDownloader(
        database,
        storage,
        settings=MediaDownloaderSettings(max_attempts=2, backoff_seconds=0),
        client=client,
        sleep=lambda _: None,
    ) as downloader:
        summary = downloader.download_batch()

    assert attempts == 2
    assert summary.uploaded == 1
    assert summary.failed == 0
    assert len(r2_client.put_calls) == 1
    client.close()


def test_network_interruption_is_retried(tmp_path):
    database = _database(
        tmp_path,
        [("photo", "https://data.exleasingcar.com/network-retry.jpg")],
    )
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("synthetic interrupted response")
        return _photo_response()

    storage, r2_client = _storage()
    client = _http_client(handler)
    with MediaDownloader(
        database,
        storage,
        settings=MediaDownloaderSettings(max_attempts=2, backoff_seconds=0),
        client=client,
        sleep=lambda _: None,
    ) as downloader:
        summary = downloader.download_batch()

    assert attempts == 2
    assert summary.uploaded == 1
    assert summary.failed == 0
    assert len(r2_client.put_calls) == 1
    client.close()


def test_pdf_signature_is_checked_within_the_first_kilobyte(tmp_path):
    database = _database(
        tmp_path,
        [("report", "https://data.exleasingcar.com/late-header.pdf")],
    )

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=(b" " * 200) + b"%PDF-1.7 synthetic report",
        )

    storage, r2_client = _storage()
    client = _http_client(handler)
    with MediaDownloader(
        database,
        storage,
        client=client,
        sleep=lambda _: None,
    ) as downloader:
        summary = downloader.download_batch()

    assert summary.uploaded == 1
    assert summary.failed == 0
    assert len(r2_client.put_calls) == 1
    client.close()


def test_r2_failure_is_retried_without_losing_the_download(tmp_path):
    database = _database(
        tmp_path,
        [("photo", "https://data.exleasingcar.com/r2-retry.jpg")],
    )
    r2_client = FakeR2Client()
    r2_client.fail_put_attempts = 1
    storage, _ = _storage(r2_client)
    client = _http_client(lambda request: _photo_response())

    with MediaDownloader(
        database,
        storage,
        settings=MediaDownloaderSettings(max_attempts=2, backoff_seconds=0),
        client=client,
        sleep=lambda _: None,
    ) as downloader:
        summary = downloader.download_batch()

    assert summary.uploaded == 1
    assert summary.failed == 0
    assert len(r2_client.put_calls) == 1
    client.close()


def test_refresh_reuses_identical_content_and_preserves_changed_versions(tmp_path):
    database = _database(
        tmp_path,
        [("photo", "https://data.exleasingcar.com/changing.jpg")],
    )
    current_body = b"\xff\xd8\xfffirst-version"
    storage, r2_client = _storage()
    client = _http_client(lambda request: _photo_response(current_body))

    with MediaDownloader(
        database,
        storage,
        settings=MediaDownloaderSettings(backoff_seconds=0),
        client=client,
        sleep=lambda _: None,
    ) as downloader:
        first = downloader.download_batch()
        same = downloader.download_batch(include_downloaded=True)
        current_body = b"\xff\xd8\xffsecond-version"
        changed = downloader.download_batch(include_downloaded=True)

    assert first.uploaded == 1
    assert same.reused == 1
    assert changed.uploaded == 1
    assert len(r2_client.put_calls) == 2

    with sqlite3.connect(database.path) as connection:
        asset_count = connection.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0]
        version_count = connection.execute(
            "SELECT COUNT(*) FROM media_asset_versions"
        ).fetchone()[0]
        hashes = {
            row[0]
            for row in connection.execute("SELECT sha256 FROM media_assets").fetchall()
        }

    expected_hashes = {
        hashlib.sha256(b"\xff\xd8\xfffirst-version").hexdigest(),
        hashlib.sha256(b"\xff\xd8\xffsecond-version").hexdigest(),
    }
    assert asset_count == 2
    assert version_count == 2
    assert hashes == expected_hashes
    client.close()


def test_invalid_http_status_is_recorded_without_exposing_source_url(tmp_path):
    secret_url = "https://data.exleasingcar.com/private/report.pdf?token=do-not-log"
    database = _database(tmp_path, [("report", secret_url)])
    storage, r2_client = _storage()
    client = _http_client(lambda request: httpx.Response(404))

    with MediaDownloader(
        database,
        storage,
        settings=MediaDownloaderSettings(backoff_seconds=0),
        client=client,
        sleep=lambda _: None,
    ) as downloader:
        summary = downloader.download_batch()

    row = database.get_lot_media_sources("19702473", "209633")[0]
    assert summary.failed == 1
    assert "do-not-log" not in str(row["last_error"])
    assert secret_url not in str(row["last_error"])
    assert r2_client.put_calls == []
    client.close()
