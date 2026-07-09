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

import io
import stat
import tarfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import boto3
from botocore.client import Config as BotoConfig

from .config import WorkerConfig

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

# Where the plugin manifest lives; used only to locate the bundle root inside an
# archive that wraps everything in one folder. Mirrors bundles._MANIFEST_LOCATIONS.
_MANIFEST_LOCATIONS = (Path(".claude-plugin") / "plugin.json", Path("plugin.json"))


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


def _has_manifest(directory: Path) -> bool:
    return any((directory / loc).is_file() for loc in _MANIFEST_LOCATIONS)


def _bundle_root(extracted: Path) -> Path:
    """The plugin root: unwrap a single top-level folder if it carries the
    manifest (matches the API's ``bundles.bundle_root``)."""
    if _has_manifest(extracted):
        return extracted
    subdirs = [p for p in extracted.iterdir() if p.is_dir()]
    if len(subdirs) == 1 and _has_manifest(subdirs[0]):
        return subdirs[0]
    return extracted


def safe_extract(data: bytes, dest: Path) -> None:
    """Extract a zip or tar(.gz) archive into ``dest``, refusing unsafe entries.

    Unsafe means anything that could write or point outside ``dest``: absolute
    paths, ``..`` traversal, and symlink entries. The tar path delegates to
    ``tarfile``'s ``filter="data"`` (py3.12+), which already rejects all three.
    The zip path has no equivalent built-in, so it is checked here: ``zipfile``
    would otherwise recreate a symlink entry verbatim, and a symlink such as
    ``config -> ../../etc/passwd`` passes a name-only ``..`` check yet escapes
    the extraction dir when followed. This is the single archive extractor for
    the worker; ``eval.stream`` imports it rather than duplicating the logic.
    """
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                name = info.filename
                if Path(name).is_absolute() or ".." in Path(name).parts:
                    raise ValueError(f"unsafe path in bundle: {name}")
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError(f"symlink entry not allowed in bundle: {name}")
            zf.extractall(dest)
        return
    open_error: tarfile.TarError | None = None
    for mode in ("r:gz", "r:"):
        try:
            tf = tarfile.open(fileobj=io.BytesIO(data), mode=mode)
        except tarfile.TarError as exc:
            open_error = exc  # wrong compression for this mode; try the next
            continue
        with tf:
            try:
                tf.extractall(dest, filter="data")
            except tarfile.FilterError as exc:
                # filter="data" rejects symlinks, absolute paths, traversal, and
                # special files. Surface it as an unsafe-entry error instead of
                # letting it fall through and be misreported as "unrecognized".
                raise ValueError(f"unsafe entry in bundle: {exc}") from exc
        return
    raise ValueError("bundle is not a recognized zip or tar archive") from open_error


def extract_bundle(data: bytes, dest: Path) -> Path:
    """Extract ``data`` into ``dest`` and return the plugin root to mount.

    The returned path is ``dest`` when the archive is flat, or its single
    wrapper subdir when the manifest sits one level down -- the same root the
    API validated, so the runner reads the plugin from the expected layout.
    """
    safe_extract(data, dest)
    return _bundle_root(dest)
