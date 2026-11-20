"""Bounded downloader for discovered Exleasingcar photos and PDF reports."""

from __future__ import annotations

import argparse
import hashlib
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote, urljoin, urlsplit

from dotenv import load_dotenv
import httpx
from typing_extensions import Self

from .database import ArchiveDatabase
from .r2_storage import CloudflareR2Storage, R2Settings

TRUSTED_MEDIA_HOSTS = frozenset({"data.exleasingcar.com"})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_ALLOWED_CONTENT_TYPES = frozenset(
    {
        "application/pdf",
        "image/avif",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)


@dataclass(frozen=True, slots=True)
class MediaDownloaderSettings:
    """Resource limits and retry policy for one bounded media batch."""

    batch_size: int = 20
    max_photo_bytes: int = 25 * 1024 * 1024
    max_report_bytes: int = 100 * 1024 * 1024
    max_redirects: int = 5
    max_attempts: int = 3
    backoff_seconds: float = 0.5
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        integer_values = {
            "batch_size": self.batch_size,
            "max_photo_bytes": self.max_photo_bytes,
            "max_report_bytes": self.max_report_bytes,
            "max_redirects": self.max_redirects,
            "max_attempts": self.max_attempts,
        }
        for name, value in integer_values.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
        if not 1 <= self.batch_size <= 1000:
            raise ValueError("batch_size must be between 1 and 1000")
        if self.max_photo_bytes <= 0 or self.max_report_bytes <= 0:
            raise ValueError("media size limits must be positive")
        if not 0 <= self.max_redirects <= 10:
            raise ValueError("max_redirects must be between 0 and 10")
        if not 1 <= self.max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        if self.backoff_seconds < 0 or self.timeout_seconds <= 0:
            raise ValueError("backoff_seconds must be non-negative and timeout positive")


@dataclass(frozen=True, slots=True)
class MediaDownloadSummary:
    """Result counts for one downloader batch."""

    selected: int
    downloaded: int
    uploaded: int
    reused: int
    failed: int
    bytes_downloaded: int


class MediaDownloadError(RuntimeError):
    """A safe, URL-free media validation or transfer failure."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class _DownloadedMedia:
    body: bytes
    sha256: str
    content_type: str
    filename: str
    byte_size: int


def _validate_media_url(value: str) -> str:
    """Accept only HTTPS URLs on the same host used by media discovery."""
    if not isinstance(value, str) or not value.strip():
        raise MediaDownloadError("empty media URL")
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError as exc:
        raise MediaDownloadError("malformed media URL") from exc
    if (
        parts.scheme.casefold() != "https"
        or (parts.hostname or "").casefold() not in TRUSTED_MEDIA_HOSTS
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or not parts.path
    ):
        raise MediaDownloadError("media URL host or scheme is not allowed")
    return value.strip()


def _detect_content_type(prefix: bytes, media_type: str) -> str:
    """Identify supported formats from their signatures, not their filenames."""
    if media_type == "report":
        if prefix[:1024].find(b"%PDF-") >= 0:
            return "application/pdf"
        raise MediaDownloadError("downloaded report is not a PDF")

    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
        return "image/webp"
    if (
        len(prefix) >= 16
        and prefix[4:8] == b"ftyp"
        and (b"avif" in prefix[8:32] or b"avis" in prefix[8:32])
    ):
        return "image/avif"
    raise MediaDownloadError("downloaded photo has an unsupported file signature")


def _safe_filename(url: str, media_type: str, source_id: int) -> str:
    raw_name = unquote(PurePosixPath(urlsplit(url).path).name)
    suffix = PurePosixPath(raw_name).suffix.lower()
    safe_suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) else (
        ".pdf" if media_type == "report" else ".img"
    )
    stem = PurePosixPath(raw_name).stem
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")[:80]
    if not safe_stem:
        safe_stem = f"media-{source_id}"
    return f"{safe_stem}{safe_suffix}"


class MediaDownloader:
    """Download a bounded batch and archive immutable content in private R2."""

    def __init__(
        self,
        database: ArchiveDatabase,
        storage: CloudflareR2Storage,
        *,
        settings: MediaDownloaderSettings | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.database = database
        self.storage = storage
        self.settings = settings or MediaDownloaderSettings()
        self._client = client or httpx.Client(
            follow_redirects=False,
            timeout=self.settings.timeout_seconds,
            headers={"User-Agent": "ExleasingcarArchive/0.3 media-archive"},
        )
        self._owns_client = client is None
        self._sleep = sleep

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def download_batch(self, *, include_downloaded: bool = False) -> MediaDownloadSummary:
        """Process at most the configured number of sources; isolate file errors."""
        sources = self.database.get_pending_media_sources(
            self.settings.batch_size,
            include_downloaded=include_downloaded,
        )
        downloaded = uploaded = reused = failed = bytes_downloaded = 0

        for source in sources:
            source_id = int(source["media_source_id"])
            media_type = str(source["media_type"])
            source_url = str(source["source_url"])
            self.database.mark_media_source_attempted(source_id)
            try:
                result = self._download_one(source_id, media_type, source_url)
            except Exception as exc:  # noqa: BLE001 - isolate all failures for this media source
                # Exception text can contain signed URLs or query parameters. Never persist it.
                safe_error = (
                    str(exc)
                    if isinstance(exc, MediaDownloadError)
                    else f"transfer failed ({type(exc).__name__})"
                )
                self.database.mark_media_source_failed(source_id, safe_error)
                failed += 1
                continue

            downloaded += 1
            bytes_downloaded += result.byte_size
            if result.uploaded:
                uploaded += 1
            else:
                reused += 1

        return MediaDownloadSummary(
            selected=len(sources),
            downloaded=downloaded,
            uploaded=uploaded,
            reused=reused,
            failed=failed,
            bytes_downloaded=bytes_downloaded,
        )

    def _download_one(
        self,
        source_id: int,
        media_type: str,
        source_url: str,
    ) -> _DownloadedMediaResult:
        if media_type not in {"photo", "report"}:
            raise MediaDownloadError("unsupported media category")
        media = self._fetch_with_retries(source_id, media_type, source_url)
        object_key = self.storage.object_key_for(media.sha256, media_type)
        uploaded = self._store_with_retries(
            media.body,
            object_key=object_key,
            content_type=media.content_type,
            sha256=media.sha256,
        )
        self.database.register_media_asset_for_source(
            source_id,
            sha256=media.sha256,
            object_key=object_key,
            mime_type=media.content_type,
            byte_size=media.byte_size,
            original_filename=media.filename,
        )
        return _DownloadedMediaResult(
            byte_size=media.byte_size,
            uploaded=uploaded,
        )

    def _fetch_with_retries(
        self,
        source_id: int,
        media_type: str,
        source_url: str,
    ) -> _DownloadedMedia:
        last_error: Exception | None = None
        for attempt in range(self.settings.max_attempts):
            try:
                return self._fetch_once(source_id, media_type, source_url)
            except MediaDownloadError as exc:
                if not exc.retryable:
                    raise
                last_error = exc
            except httpx.TransportError as exc:
                last_error = exc
            if attempt + 1 < self.settings.max_attempts:
                self._sleep(self.settings.backoff_seconds * (2**attempt))
        raise MediaDownloadError(
            f"media request failed after {self.settings.max_attempts} attempts "
            f"({type(last_error).__name__ if last_error else 'network error'})"
        )

    def _fetch_once(
        self,
        source_id: int,
        media_type: str,
        source_url: str,
    ) -> _DownloadedMedia:
        current_url = _validate_media_url(source_url)
        max_bytes = (
            self.settings.max_photo_bytes
            if media_type == "photo"
            else self.settings.max_report_bytes
        )

        for redirect_count in range(self.settings.max_redirects + 1):
            with self._client.stream("GET", current_url) as response:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise MediaDownloadError("redirect response has no location")
                    if redirect_count >= self.settings.max_redirects:
                        raise MediaDownloadError("media URL exceeded redirect limit")
                    current_url = _validate_media_url(urljoin(current_url, location))
                    continue

                if response.status_code in _RETRYABLE_STATUSES:
                    raise MediaDownloadError(
                        f"temporary HTTP status {response.status_code}",
                        retryable=True,
                    )
                if not 200 <= response.status_code < 300:
                    raise MediaDownloadError(
                        f"media server returned HTTP {response.status_code}"
                    )

                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError as exc:
                        raise MediaDownloadError("invalid content length") from exc
                    if declared_length < 0 or declared_length > max_bytes:
                        raise MediaDownloadError("media file exceeds configured size limit")

                header_type = (
                    response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if header_type not in _ALLOWED_CONTENT_TYPES:
                    raise MediaDownloadError("media server returned an unsupported content type")
                if media_type == "report" and header_type != "application/pdf":
                    raise MediaDownloadError("report response is not labelled as PDF")
                if media_type == "photo" and not header_type.startswith("image/"):
                    raise MediaDownloadError("photo response is not labelled as an image")

                digest = hashlib.sha256()
                total_bytes = 0
                prefix = bytearray()
                with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as buffer:
                    for chunk in response.iter_bytes(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        total_bytes += len(chunk)
                        if total_bytes > max_bytes:
                            raise MediaDownloadError(
                                "media file exceeds configured size limit"
                            )
                        if len(prefix) < 1024:
                            prefix.extend(chunk[: 1024 - len(prefix)])
                        digest.update(chunk)
                        buffer.write(chunk)

                    if total_bytes == 0:
                        raise MediaDownloadError("media response is empty")
                    detected_type = _detect_content_type(bytes(prefix), media_type)
                    if detected_type != header_type:
                        raise MediaDownloadError(
                            "response content type does not match the file signature"
                        )
                    buffer.seek(0)
                    body = buffer.read()

                return _DownloadedMedia(
                    body=body,
                    sha256=digest.hexdigest(),
                    content_type=detected_type,
                    filename=_safe_filename(current_url, media_type, source_id),
                    byte_size=total_bytes,
                )

        raise MediaDownloadError("media redirect handling failed")

    def _store_with_retries(
        self,
        body: bytes,
        *,
        object_key: str,
        content_type: str,
        sha256: str,
    ) -> bool:
        last_error: Exception | None = None
        for attempt in range(self.settings.max_attempts):
            try:
                if self.storage.object_exists(object_key):
                    return False
                self.storage.put_bytes(
                    body,
                    object_key=object_key,
                    content_type=content_type,
                    expected_sha256=sha256,
                )
                return True
            except Exception as exc:  # noqa: BLE001 - boto3 clients expose varied SDK exceptions
                last_error = exc
                if attempt + 1 < self.settings.max_attempts:
                    self._sleep(self.settings.backoff_seconds * (2**attempt))
        raise MediaDownloadError(
            f"R2 storage failed after {self.settings.max_attempts} attempts "
            f"({type(last_error).__name__ if last_error else 'storage error'})"
        )


@dataclass(frozen=True, slots=True)
class _DownloadedMediaResult:
    byte_size: int
    uploaded: bool


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Download a bounded batch of discovered lot photos and PDF reports into private R2."
    )
    parser.add_argument("--database", default="data/fresh_api_test.sqlite3")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--max-photo-bytes", type=int, default=25 * 1024 * 1024)
    parser.add_argument("--max-report-bytes", type=int, default=100 * 1024 * 1024)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Recheck sources that already have an archived version.",
    )
    args = parser.parse_args(argv)

    settings = MediaDownloaderSettings(
        batch_size=args.batch_size,
        max_photo_bytes=args.max_photo_bytes,
        max_report_bytes=args.max_report_bytes,
        max_attempts=args.max_attempts,
    )
    database = ArchiveDatabase(args.database)
    database.initialize()
    storage = CloudflareR2Storage(R2Settings.from_env())

    with MediaDownloader(database, storage, settings=settings) as downloader:
        summary = downloader.download_batch(include_downloaded=args.refresh)

    print(
        "Media batch: "
        f"selected={summary.selected}, downloaded={summary.downloaded}, "
        f"uploaded={summary.uploaded}, reused={summary.reused}, "
        f"failed={summary.failed}, bytes={summary.bytes_downloaded}"
    )
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
