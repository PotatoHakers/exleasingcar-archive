"""Read-only account-wide R2 usage from Cloudflare's GraphQL Analytics API."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .r2_budget import R2UsageSnapshot

_GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"
_ACCOUNT_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_MAX_STORAGE_METRIC_AGE = timedelta(hours=72)

_CLASS_A_ACTIONS = frozenset(
    {
        "ListBuckets",
        "PutBucket",
        "ListObjects",
        "ListObjectsV2",
        "PutObject",
        "CopyObject",
        "CompleteMultipartUpload",
        "CreateMultipartUpload",
        "LifecycleStorageTierTransition",
        "ListMultipartUploads",
        "UploadPart",
        "UploadPartCopy",
        "ListParts",
        "PutBucketEncryption",
        "PutBucketCors",
        "PutBucketLifecycleConfiguration",
    }
)
_CLASS_B_ACTIONS = frozenset(
    {
        "HeadBucket",
        "HeadObject",
        "GetObject",
        "UsageSummary",
        "GetBucketEncryption",
        "GetBucketLocation",
        "GetBucketCors",
        "GetBucketLifecycleConfiguration",
    }
)
_FREE_ACTIONS = frozenset({"DeleteObject", "DeleteBucket", "AbortMultipartUpload"})

_QUERY = """
query R2BudgetUsage($accountTag: string!, $startDate: Time, $endDate: Time) {
  viewer {
    accounts(filter: {accountTag: $accountTag}) {
      r2OperationsAdaptiveGroups(
        limit: 10000
        filter: {datetime_geq: $startDate, datetime_leq: $endDate}
      ) {
        sum { requests }
        dimensions { actionType }
      }
      r2StorageAdaptiveGroups(
        limit: 10000
        filter: {datetime_geq: $startDate, datetime_leq: $endDate}
        orderBy: [datetime_DESC]
      ) {
        max { payloadSize metadataSize }
        dimensions { datetime bucketName }
      }
    }
  }
}
"""


class R2AnalyticsError(RuntimeError):
    """Raised when account-level R2 usage cannot be verified."""


class CloudflareR2Analytics:
    """Fetch account-wide usage; missing or invalid analytics must stop R2 work."""

    def __init__(
        self,
        account_id: str,
        api_token: str,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        if not _ACCOUNT_ID_RE.fullmatch(account_id):
            raise ValueError("Cloudflare account ID must contain 32 hexadecimal characters")
        if not api_token.strip():
            raise ValueError("Cloudflare Account Analytics token cannot be empty")
        self.account_id = account_id
        self._api_token = api_token
        self._client = client or httpx.Client(timeout=20)
        self._owns_client = client is None

    @classmethod
    def from_env(
        cls,
        environ: dict[str, str] | None = None,
    ) -> CloudflareR2Analytics:
        values = os.environ if environ is None else environ
        account_id = values.get("EXLEASINGCAR_R2_ACCOUNT_ID", "").strip()
        token = values.get("EXLEASINGCAR_CF_ACCOUNT_ANALYTICS_TOKEN", "").strip()
        if not account_id:
            raise RuntimeError("Missing EXLEASINGCAR_R2_ACCOUNT_ID")
        if not token:
            raise RuntimeError(
                "Missing EXLEASINGCAR_CF_ACCOUNT_ANALYTICS_TOKEN; "
                "R2 activity is blocked until account analytics can be checked"
            )
        return cls(account_id, token)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> CloudflareR2Analytics:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def fetch_usage(self) -> R2UsageSnapshot:
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=31)
        payload = {
            "query": _QUERY,
            "variables": {
                "accountTag": self.account_id,
                "startDate": start.isoformat().replace("+00:00", "Z"),
                "endDate": now.isoformat().replace("+00:00", "Z"),
            },
        }
        try:
            response = self._client.post(
                _GRAPHQL_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_token}",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            result: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise R2AnalyticsError(
                "Cloudflare R2 analytics is unavailable; R2 activity is blocked"
            ) from exc

        if result.get("errors"):
            raise R2AnalyticsError(
                "Cloudflare rejected the R2 analytics query; R2 activity is blocked"
            )
        try:
            accounts = result["data"]["viewer"]["accounts"]
            if not isinstance(accounts, list) or len(accounts) != 1:
                raise ValueError("account analytics result is missing")
            account = accounts[0]
            operation_rows = account["r2OperationsAdaptiveGroups"]
            storage_rows = account["r2StorageAdaptiveGroups"]
            if len(operation_rows) >= 10000 or len(storage_rows) >= 10000:
                raise ValueError("analytics result reached the query row limit")
            if not storage_rows:
                raise R2AnalyticsError(
                    "Cloudflare returned no R2 storage metrics; R2 activity is blocked"
                )

            class_a = class_b = 0
            for row in operation_rows:
                action = str(row["dimensions"]["actionType"])
                requests = int(row["sum"]["requests"] or 0)
                if requests < 0:
                    raise ValueError("negative operation count")
                if action in _CLASS_A_ACTIONS:
                    class_a += requests
                elif action in _CLASS_B_ACTIONS:
                    class_b += requests
                elif action in _FREE_ACTIONS:
                    continue
                else:
                    # Unknown actions are charged to both counters conservatively.
                    class_a += requests
                    class_b += requests

            bucket_peaks: dict[str, int] = {}
            latest_storage_metric: datetime | None = None
            for index, row in enumerate(storage_rows):
                dimensions = row.get("dimensions") or {}
                bucket_name = str(dimensions.get("bucketName") or f"unknown-{index}")
                metric_time = dimensions.get("datetime")
                if metric_time:
                    parsed_time = datetime.fromisoformat(
                        str(metric_time).replace("Z", "+00:00")
                    )
                    if parsed_time.tzinfo is None:
                        parsed_time = parsed_time.replace(tzinfo=timezone.utc)
                    if latest_storage_metric is None or parsed_time > latest_storage_metric:
                        latest_storage_metric = parsed_time
                maximum = row.get("max") or {}
                payload_size = int(maximum.get("payloadSize") or 0)
                metadata_size = int(maximum.get("metadataSize") or 0)
                if payload_size < 0 or metadata_size < 0:
                    raise ValueError("negative storage metric")
                bucket_peaks[bucket_name] = max(
                    bucket_peaks.get(bucket_name, 0),
                    payload_size + metadata_size,
                )

            if latest_storage_metric is None:
                raise R2AnalyticsError(
                    "Cloudflare returned storage metrics without timestamps; R2 activity is blocked"
                )
            if now - latest_storage_metric > _MAX_STORAGE_METRIC_AGE:
                raise R2AnalyticsError(
                    "R2 storage analytics is older than 72 hours; R2 activity is blocked"
                )
            return R2UsageSnapshot(
                class_a_requests=class_a,
                class_b_requests=class_b,
                storage_peak_bytes=sum(bucket_peaks.values()),
            )
        except R2AnalyticsError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise R2AnalyticsError(
                "Cloudflare returned incomplete R2 analytics; R2 activity is blocked"
            ) from exc
