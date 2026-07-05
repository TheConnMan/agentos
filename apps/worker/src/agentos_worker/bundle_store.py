"""Read-only access to plugin bundles in MinIO/S3 (mirrors the API's BundleStore).

The eval consumer fetches a version's immutable bundle by its bundle_ref key and
extracts it to read the bundle's own eval suite (evals/cases.json). Uses boto3
with path-style addressing (MinIO), the same construction the API's write path
uses, so the env names line up.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import boto3
from botocore.client import Config as BotoConfig

from .config import WorkerConfig

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


class BundleStore:
    """Fetches bundle bytes by key from the bundles bucket."""

    def __init__(self, config: WorkerConfig) -> None:
        self._bucket = config.bundle_bucket
        self._client: S3Client = boto3.client(
            "s3",
            endpoint_url=config.s3_endpoint_url,
            aws_access_key_id=config.s3_access_key,
            aws_secret_access_key=config.s3_secret_key,
            region_name=config.s3_region,
            config=BotoConfig(s3={"addressing_style": "path"}),
        )

    def get(self, key: str) -> bytes:
        """Fetch the object bytes for ``key``. Raises on a missing key or S3 error
        (the caller treats any failure as an unresolvable suite)."""
        obj = self._client.get_object(Bucket=self._bucket, Key=key)
        body: bytes = obj["Body"].read()
        return body
