"""Shared bundle persistence used by the upload endpoint (B2) and git flow (J1).

Validation and storage are split so a caller can reject an invalid bundle before
creating any database rows, then store the validated bytes under the immutable
per-version key.
"""

import hashlib
import tempfile
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from . import bundles, crud
from .models import AgentVersion
from .schemas import BundleOut
from .storage import BundleStore


class BundleInvalid(Exception):
    """A bundle failed plugin-format validation; carries the actionable errors."""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__("bundle failed validation")
        self.errors = errors


def validate_archive(data: bytes) -> tuple[str, str]:
    """Validate an archive's bytes. Returns (extension, content_type).

    Raises ``bundles.UnsupportedArchive`` if the bytes are not a zip/tar(.gz),
    and ``BundleInvalid`` if the plugin bundle fails validation.
    """

    with tempfile.TemporaryDirectory() as tmp:
        extension, content_type, result = bundles.extract_and_validate(
            data, Path(tmp)
        )
    if not result.valid:
        raise BundleInvalid([e.model_dump() for e in result.errors])
    return extension, content_type


async def store_bundle(
    store: BundleStore,
    session: AsyncSession,
    agent_id: uuid.UUID,
    version: AgentVersion,
    data: bytes,
    extension: str,
    content_type: str,
) -> BundleOut:
    """Store validated bytes under the immutable key and record them."""

    key = f"bundles/{agent_id}/{version.id}{extension}"
    digest = hashlib.sha256(data).hexdigest()
    await store.put(key, data, content_type)
    await crud.attach_bundle(session, version, key, digest)
    return BundleOut(
        version_id=version.id,
        bundle_ref=key,
        bundle_sha256=digest,
        size_bytes=len(data),
    )
