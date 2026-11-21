"""Private Cloudflare R2 storage for content-addressed archive media.

The bucket must remain private. This adapter never sets a public ACL and only
creates short-lived, object-specific presigned GET URLs on demand.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any

from .r2_budget import R2BudgetExceeded, R2UsageBudget

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_ALLOWED_MEDIA_TYPES = frozenset(
    {
        "application/pdf",
        "image/avif",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)
_ALLOWED_CATEGORIES = frozenset({"photo", "report"})


@dataclass(frozen=True, slots=True)
class R2Settings:
    """Credentials and endpoint needed for one private R2 bucket."""

    account_id: str
    bucket_name: str
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-fA-F]{32}", self.account_id):
            raise ValueError("R2 account ID must contain 32 hexadecimal characters")
        if not _BUCKET_RE.fullmatch(self.bucket_name):
            raise ValueError("R2 bucket name has an invalid format")
        if not self.access_key_id.strip():
            raise ValueError("R2 access key ID cannot be empty")
        if not self.secret_access_key.strip():
            raise ValueError("R2 secret access key cannot be empty")

    @property
    def endpoint_url(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> R2Settings:
        """Read credentials from process environment without exposing values."""
        values = os.environ if environ is None else environ
        names = {
            "account_id": "EXLEASINGCAR_R2_ACCOUNT_ID",
            "bucket_name": "EXLEASINGCAR_R2_BUCKET",
            "access_key_id": "EXLEASINGCAR_R2_ACCESS_KEY_ID",
            "secret_access_key": "EXLEASINGCAR_R2_SECRET_ACCESS_KEY",
        }
        missing = [env_name for env_name in names.values() if not values.get(env_name)]
        if missing:
            raise RuntimeError(
                "Missing Cloudflare R2 environment variables: " + ", ".join(missing)
            )
        return cls(
            account_id=values[names["account_id"]].strip(),
            bucket_name=values[names["bucket_name"]].strip(),
            access_key_id=values[names["access_key_id"]].strip(),
            secret_access_key=values[names["secret_access_key"]].strip(),
        )


class CloudflareR2Storage:
    """Small S3-compatible adapter; it deliberately has no public-ACL methods."""

    def __init__(
        self,
        settings: R2Settings,
        client: Any | None = None,
        *,
        budget: R2UsageBudget,
    ) -> None:
        if not isinstance(budget, R2UsageBudget):
            raise TypeError("budget must be an R2UsageBudget instance")
        self.settings = settings
        self._budget = budget
        if client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:  # pragma: no cover - exercised in deployments
                raise RuntimeError(
                    "Cloudflare R2 support requires boto3; install "
                    "requirements-collector.txt"
                ) from exc
            client = boto3.client(
                "s3",
                endpoint_url=settings.endpoint_url,
                aws_access_key_id=settings.access_key_id,
                aws_secret_access_key=settings.secret_access_key,
                region_name="auto",
                config=Config(
                    signature_version="s3v4",
                    retries={"mode": "standard", "total_max_attempts": 1},
                ),
            )
        self._client = client

    @staticmethod
    def validate_object_key(object_key: str) -> str:
        if not isinstance(object_key, str) or not object_key.strip():
            raise ValueError("object key cannot be empty")
        normalized = object_key.strip().replace("\\", "/")
        if (
            normalized.startswith("/")
            or "\x00" in normalized
            or any(part in {"", ".", ".."} for part in normalized.split("/"))
        ):
            raise ValueError("object key must be a safe relative path")
        return normalized

    @staticmethod
    def object_key_for(sha256: str, media_type: str) -> str:
        """Return a deterministic immutable key for one content hash."""
        if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
            raise ValueError("sha256 must be a lowercase 64-character hex digest")
        if media_type not in _ALLOWED_CATEGORIES:
            raise ValueError("media_type must be 'photo' or 'report'")
        return f"archive/{media_type}/{sha256[:2]}/{sha256}"

    def put_bytes(
        self,
        body: bytes,
        *,
        object_key: str,
        content_type: str,
        expected_sha256: str | None = None,
    ) -> str:
        """Upload validated bytes using a content-addressed key.

        Existing content is not overwritten with different bytes: the digest
        embedded in the key must match the payload before the upload starts.
        """
        if not isinstance(body, bytes):
            raise TypeError("body must be bytes")
        normalized_key = self.validate_object_key(object_key)
        normalized_type = content_type.split(";", 1)[0].strip().lower()
        if normalized_type not in _ALLOWED_MEDIA_TYPES:
            raise ValueError(f"Unsupported media content type: {normalized_type!r}")

        digest = hashlib.sha256(body).hexdigest()
        if expected_sha256 is not None:
            if not _SHA256_RE.fullmatch(expected_sha256):
                raise ValueError("expected_sha256 must be a lowercase SHA-256 digest")
            if digest != expected_sha256:
                raise ValueError("Payload SHA-256 does not match expected_sha256")
        if normalized_key.rsplit("/", 1)[-1] != digest:
            raise ValueError("Payload SHA-256 does not match object key")

        self._budget.reserve_put(normalized_key, len(body))
        self._client.put_object(
            Bucket=self.settings.bucket_name,
            Key=normalized_key,
            Body=body,
            ContentLength=len(body),
            ContentType=normalized_type,
            Metadata={"sha256": digest},
        )
        return normalized_key

    def object_exists(self, object_key: str) -> bool:
        """Check whether an object exists without downloading its contents."""
        normalized_key = self.validate_object_key(object_key)
        self._budget.reserve_operation("class_b")
        try:
            self._client.head_object(
                Bucket=self.settings.bucket_name,
                Key=normalized_key,
            )
        except Exception as exc:
            response = getattr(exc, "response", {})
            error = response.get("Error", {}) if isinstance(response, dict) else {}
            code = str(error.get("Code", ""))
            status = (
                response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if isinstance(response, dict)
                else None
            )
            if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
                return False
            raise
        return True

    def presign_get_url(
        self,
        object_key: str,
        *,
        expires_in_seconds: int = 300,
    ) -> str:
        """Refuse direct reads because browser GETs bypass the local budget ledger."""
        raise R2BudgetExceeded(
            "Direct presigned R2 reads are disabled by the free-tier guard; "
            "serve media through a guarded application-side read endpoint"
        )
