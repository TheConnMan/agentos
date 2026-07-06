"""Agents and their versions."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from starlette.concurrency import run_in_threadpool

from .. import bundles, crud
from ..auth import require_api_key
from ..deps import SessionDep, StoreDep
from ..schemas import (
    AgentCreate,
    AgentOut,
    AgentUpdate,
    BundleFile,
    BundleFiles,
    VersionCreate,
    VersionOut,
)

router = APIRouter(
    prefix="/agents", tags=["agents"], dependencies=[Depends(require_api_key)]
)


@router.post("", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
async def create_agent(data: AgentCreate, session: SessionDep) -> AgentOut:
    agent = await crud.create_agent(session, data)
    return AgentOut.model_validate(agent)


@router.get("", response_model=list[AgentOut])
async def list_agents(session: SessionDep) -> list[AgentOut]:
    agents = await crud.list_agents(session)
    return [AgentOut.model_validate(a) for a in agents]


@router.get("/{agent_id}", response_model=AgentOut)
async def get_agent(agent_id: uuid.UUID, session: SessionDep) -> AgentOut:
    agent = await crud.get_agent(session, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    return AgentOut.model_validate(agent)


@router.patch("/{agent_id}", response_model=AgentOut)
async def update_agent(
    agent_id: uuid.UUID, data: AgentUpdate, session: SessionDep
) -> AgentOut:
    # Lets a redeploy move an existing agent's Slack channel (the CLI only sends
    # this when --slack-channel was passed explicitly). An omitted field is a
    # no-op so the agent's current channel is preserved.
    agent = await crud.get_agent(session, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    if data.slack_channel is not None:
        agent = await crud.update_agent_channel(session, agent, data.slack_channel)
    return AgentOut.model_validate(agent)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(agent_id: uuid.UUID, session: SessionDep) -> None:
    # Deleting an agent cascades its versions and deployments rows (bundle
    # objects in MinIO are left as-is, out of scope). Refuse while a deployment
    # is still active so a live agent cannot be pulled out from under Slack
    # traffic; the caller must stop it (kill/undeploy) first.
    agent = await crud.get_agent(session, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    if await crud.agent_has_active_deployment(session, agent_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "agent has an active deployment; stop it before deleting",
        )
    await crud.delete_agent(session, agent_id)


@router.post(
    "/{agent_id}/versions",
    response_model=VersionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_version(
    agent_id: uuid.UUID, data: VersionCreate, session: SessionDep
) -> VersionOut:
    if await crud.get_agent(session, agent_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    version = await crud.create_version(session, agent_id, data)
    return VersionOut.model_validate(version)


@router.get("/{agent_id}/versions", response_model=list[VersionOut])
async def list_versions(
    agent_id: uuid.UUID, session: SessionDep
) -> list[VersionOut]:
    if await crud.get_agent(session, agent_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    versions = await crud.list_versions(session, agent_id)
    return [VersionOut.model_validate(v) for v in versions]


@router.get(
    "/{agent_id}/versions/{version_id}/files", response_model=BundleFiles
)
async def read_version_files(
    agent_id: uuid.UUID,
    version_id: uuid.UUID,
    session: SessionDep,
    store: StoreDep,
) -> BundleFiles:
    # The UI reads a version's authored text (skills, manifest, eval cases) to
    # render the bundle without pulling the raw archive. 404 covers a missing
    # agent, a version that is not this agent's, and a version with no bundle
    # stored yet -- there is nothing to read in any of those cases.
    version = await crud.get_version(session, version_id)
    if version is None or version.agent_id != agent_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "version not found")
    if version.bundle_ref is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "no bundle stored for this version"
        )
    data = await store.get(version.bundle_ref)
    files = await run_in_threadpool(bundles.read_bundle_text_files, data)
    return BundleFiles(files=[BundleFile(path=p, content=c) for p, c in files])
