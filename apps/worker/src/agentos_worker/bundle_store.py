"""Read-only access to plugin bundles in MinIO/S3 (mirrors the API's BundleStore).

The eval consumer fetches a version's immutable bundle by its bundle_ref key and
extracts it to read the bundle's own eval suite (evals/cases.json). Uses boto3
with path-style addressing (MinIO), the same construction the API's write path
uses, so the env names line up.

``extract_bundle`` is the Docker-substrate counterpart to the Kubernetes
bundle-fetch/extract init pair: with no init containers, the worker fetches and
unpacks the bundle itself and bind-mounts the result as the runner's plugin dir.
Its unwrap semantics mirror the API's ``bundles.bundle_root`` exactly (unwrap a
single top-level wrapper dir when that subdir carries the plugin manifest), so
the plugin root the runner sees matches the root the API validated on upload.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import boto3
from botocore.client import Config as BotoConfig
from plugin_format import bundle_root, safe_extract

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


def extract_bundle(data: bytes, dest: Path) -> Path:
    """Extract ``data`` into ``dest`` and return the plugin root to mount.

    The returned path is ``dest`` when the archive is flat, or its single
    wrapper subdir when the manifest sits one level down -- the same root the
    API validated, so the runner reads the plugin from the expected layout.
    Extraction and unwrap route through ``plugin_format`` (the single audited
    home for the traversal/symlink/special-file guards); an unsafe or
    unrecognized archive raises ``plugin_format.UnsupportedArchive``, which the
    Docker-substrate caller already treats as a fetch failure.
    """
    safe_extract(data, dest)
    return bundle_root(dest)
