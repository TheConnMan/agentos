"""Immutable bundle storage on MinIO/S3.

Bundles are written once per (agent, version) under a deterministic key and never
mutated (B2 constraint: immutability = write-once key, no dedup/GC/signing). boto3
is synchronous, so each call is offloaded to a worker thread to keep the event
loop free.
"""

from typing import TYPE_CHECKING

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError
from starlette.concurrency import run_in_threadpool

from .config import Settings

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


class BundleStore:
    def __init__(self, settings: Settings) -> None:
        self._bucket = settings.bundle_bucket
        self._client: S3Client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            config=BotoConfig(s3={"addressing_style": "path"}),
        )

    async def ensure_bucket(self) -> None:
        await run_in_threadpool(self._ensure_bucket_sync)

    def _ensure_bucket_sync(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError:
            self._client.create_bucket(Bucket=self._bucket)

    async def exists(self, key: str) -> bool:
        return await run_in_threadpool(self._exists_sync, key)

    def _exists_sync(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError:
            return False
        return True

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await run_in_threadpool(self._put_sync, key, data, content_type)

    def _put_sync(self, key: str, data: bytes, content_type: str) -> None:
        self._client.put_object(
            Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
        )

    async def get(self, key: str) -> bytes:
        return await run_in_threadpool(self._get_sync, key)

    def _get_sync(self, key: str) -> bytes:
        obj = self._client.get_object(Bucket=self._bucket, Key=key)
        body: bytes = obj["Body"].read()
        return body
