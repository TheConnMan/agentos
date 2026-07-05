"""Plugin bundle upload and fetch-by-version.

Upload validates the archive via the frozen plugin_format validator, then stores
the original bytes immutably (write-once per version). Fetch returns those exact
bytes so a runner can pull a bundle by version.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, status

from .. import bundles, crud, deploy
from ..auth import require_api_key
from ..deps import SessionDep, StoreDep
from ..models import AgentVersion
from ..schemas import BundleOut

router = APIRouter(
    prefix="/agents/{agent_id}/versions/{version_id}/bundle",
    tags=["bundles"],
    dependencies=[Depends(require_api_key)],
)

# Stored key extension -> content type, longest suffix first.
_CONTENT_TYPES = (
    (".tar.gz", "application/gzip"),
    (".tar", "application/x-tar"),
    (".zip", "application/zip"),
)


async def _load_version(
    session: SessionDep, agent_id: uuid.UUID, version_id: uuid.UUID
) -> AgentVersion:
    version = await crud.get_version(session, version_id)
    if version is None or version.agent_id != agent_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "version not found")
    return version


def _content_type_for(key: str) -> str:
    for suffix, content_type in _CONTENT_TYPES:
        if key.endswith(suffix):
            return content_type
    return "application/octet-stream"


@router.put("", response_model=BundleOut, status_code=status.HTTP_201_CREATED)
async def upload_bundle(
    agent_id: uuid.UUID,
    version_id: uuid.UUID,
    session: SessionDep,
    store: StoreDep,
    file: UploadFile,
) -> BundleOut:
    version = await _load_version(session, agent_id, version_id)
    if version.bundle_ref is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "bundle already stored for this version (bundles are immutable)",
        )

    data = await file.read()
    try:
        extension, content_type = deploy.validate_archive(data)
    except bundles.UnsupportedArchive as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except deploy.BundleInvalid as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"detail": "bundle failed validation", "errors": exc.errors},
        ) from exc

    return await deploy.store_bundle(
        store, session, agent_id, version, data, extension, content_type
    )


@router.get("")
async def download_bundle(
    agent_id: uuid.UUID,
    version_id: uuid.UUID,
    session: SessionDep,
    store: StoreDep,
) -> Response:
    version = await _load_version(session, agent_id, version_id)
    if version.bundle_ref is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "no bundle stored for this version"
        )
    data = await store.get(version.bundle_ref)
    return Response(content=data, media_type=_content_type_for(version.bundle_ref))
