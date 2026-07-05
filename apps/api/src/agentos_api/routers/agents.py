"""Agents and their versions."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from .. import crud
from ..auth import require_api_key
from ..deps import SessionDep
from ..schemas import AgentCreate, AgentOut, VersionCreate, VersionOut

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
