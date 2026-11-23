from __future__ import annotations

import hashlib
import sqlite3
from io import BytesIO

import httpx
import pytest
from PIL import Image

from exleasingcar_archive.database import ArchiveDatabase
from exleasingcar_archive.media_downloader import (
    MediaDownloader,
    MediaDownloaderSettings,
    main,
)
from exleasingcar_archive.r2_budget import R2BudgetExceeded, R2UsageBudget, R2UsageSnapshot
from exleasingcar_archive.r2_storage import CloudflareR2Storage, R2Settings


class _ChunkStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        yield from self.chunks

    def close(self):
        pass


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
        budget=R2UsageBudget(
            ":memory:", snapshot=R2UsageSnapshot(0, 0, 0)
        ),
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
    budget_report = storage._budget.report()
    assert budget_report["class_a_used"] == 1
    assert budget_report["class_b_used"] == 1
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
            stream=_ChunkStream([b"\xff\xd8\xff", b"x" * 32]),
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
    assert summary.preview_uploaded == 0
    assert summary.preview_reused == 0
    assert summary.preview_failed == 0
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
    retry_budget = storage._budget.report()
    assert retry_budget["class_a_used"] == 2
    assert retry_budget["class_b_used"] == 2
    assert retry_budget["storage_used_bytes"] == len(_photo_response().content)
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


def _valid_jpeg(width=1600, height=900):
    output = BytesIO()
    image = Image.new("RGB", (width, height), (35, 105, 175))
    image.save(output, format="JPEG", quality=90)
    image.close()
    return output.getvalue()


def test_photo_download_generates_and_reuses_card_and_gallery_previews(tmp_path):
    database = _database(
        tmp_path,
        [("photo", "https://data.exleasingcar.com/car.jpg")],
    )
    original_body = _valid_jpeg()
    storage, r2_client = _storage()
    client = _http_client(lambda request: _photo_response(original_body))

    with MediaDownloader(
        database,
        storage,
        settings=MediaDownloaderSettings(backoff_seconds=0),
        client=client,
        sleep=lambda _: None,
    ) as downloader:
        first = downloader.download_batch()
        second = downloader.download_batch(include_downloaded=True)

    assert first.uploaded == 1
    assert first.preview_uploaded == 2
    assert first.preview_reused == 0
    assert first.preview_failed == 0
    assert second.reused == 1
    assert second.preview_uploaded == 0
    assert second.preview_reused == 2
    assert second.preview_failed == 0
    assert len(r2_client.put_calls) == 3
    assert len(r2_client.objects) == 3

    original_sha = hashlib.sha256(original_body).hexdigest()
    original_asset = database.get_media_asset_by_sha256(original_sha)
    assert original_asset is not None
    source_asset_id = int(original_asset["id"])

    source_rows = database.get_lot_media_sources("19702473", "209633")
    assert len(source_rows) == 1
    assert source_rows[0]["sha256"] == original_sha
    assert source_rows[0]["mime_type"] == "image/jpeg"

    with sqlite3.connect(database.path) as connection:
        derivatives = connection.execute(
            """
            SELECT d.variant, d.status, d.width, d.height, asset.sha256
            FROM media_derivatives AS d
            JOIN media_assets AS asset ON asset.id = d.derived_asset_id
            WHERE d.source_asset_id = ?
            ORDER BY d.variant
            """,
            (source_asset_id,),
        ).fetchall()

    assert {row[0] for row in derivatives} == {"card", "gallery"}
    assert all(row[1] == "ready" for row in derivatives)
    assert all(row[2] <= 1280 and row[3] <= 960 for row in derivatives)
    assert all(len(row[4]) == 64 for row in derivatives)
    assert r2_client.objects[storage.object_key_for(original_sha, "photo")] == original_body
    client.close()


def test_invalid_photo_keeps_original_and_records_preview_failure(tmp_path):
    database = _database(
        tmp_path,
        [("photo", "https://data.exleasingcar.com/invalid.jpg")],
    )
    invalid_body = b"\xff\xd8\xffsynthetic-not-an-image"
    storage, r2_client = _storage()
    client = _http_client(lambda request: _photo_response(invalid_body))

    with MediaDownloader(
        database,
        storage,
        settings=MediaDownloaderSettings(backoff_seconds=0),
        client=client,
        sleep=lambda _: None,
    ) as downloader:
        summary = downloader.download_batch()

    assert summary.uploaded == 1
    assert summary.failed == 0
    assert summary.preview_uploaded == 0
    assert summary.preview_failed == 2
    assert len(r2_client.put_calls) == 1

    original = database.get_media_asset_by_sha256(
        hashlib.sha256(invalid_body).hexdigest()
    )
    assert original is not None
    with sqlite3.connect(database.path) as connection:
        states = connection.execute(
            """
            SELECT status, COUNT(*)
            FROM media_derivatives
            WHERE source_asset_id = ?
            GROUP BY status
            """,
            (int(original["id"]),),
        ).fetchall()
    assert len(states) == 1
    assert states[0][0] in {"unsupported", "failed"}
    assert states[0][1] == 2
    client.close()


def test_direct_presigned_get_is_blocked_by_the_budget_guard():
    storage, _ = _storage()
    digest = hashlib.sha256(b"private-media").hexdigest()
    object_key = f"archive/photo/{digest[:2]}/{digest}"

    with pytest.raises(R2BudgetExceeded, match="Direct presigned R2 reads are disabled"):
        storage.presign_get_url(object_key)



def test_cli_dry_run_previews_queue_without_cloudflare_or_media_requests(
    tmp_path, monkeypatch, capsys
):
    database = _database(
        tmp_path,
        [
            ("photo", "https://data.exleasingcar.com/one.jpg"),
            ("report", "https://data.exleasingcar.com/two.pdf"),
        ],
    )
    monkeypatch.setattr(
        "exleasingcar_archive.media_downloader.load_dotenv",
        lambda: None,
    )
    for name in (
        "EXLEASINGCAR_R2_ACCOUNT_ID",
        "EXLEASINGCAR_R2_BUCKET",
        "EXLEASINGCAR_R2_ACCESS_KEY_ID",
        "EXLEASINGCAR_R2_SECRET_ACCESS_KEY",
        "EXLEASINGCAR_CF_ACCOUNT_ANALYTICS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    exit_code = main(
        [
            "--database",
            str(database.path),
            "--batch-size",
            "1",
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "no downloads or Cloudflare/R2 requests" in output
    assert "Selected 1 source(s)" in output
    assert "Photos: 1; PDF reports: 0" in output
    assert "209633: 1 photo(s), 0 report(s)" in output
    assert "data.exleasingcar.com" not in output
    assert "19702473" not in output

    with sqlite3.connect(database.path) as connection:
        rows = connection.execute(
            """
            SELECT download_status, last_attempted_at
            FROM lot_media_sources
            ORDER BY id
            """
        ).fetchall()
    assert rows == [("discovered", None), ("discovered", None)]


def test_cli_dry_run_requires_an_existing_database_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "exleasingcar_archive.media_downloader.load_dotenv",
        lambda: None,
    )

    exit_code = main(
        [
            "--database",
            str(tmp_path / "missing.sqlite3"),
            "--dry-run",
        ]
    )

    assert exit_code == 2
    assert "database file does not exist" in capsys.readouterr().out
