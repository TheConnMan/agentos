"""Deployments of a version to an environment."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from .. import crud
from ..auth import require_api_key
from ..deps import SessionDep
from ..schemas import DeploymentCreate, DeploymentOut

router = APIRouter(
    prefix="/deployments",
    tags=["deployments"],
    dependencies=[Depends(require_api_key)],
)


@router.post("", response_model=DeploymentOut, status_code=status.HTTP_201_CREATED)
async def create_deployment(
    data: DeploymentCreate, session: SessionDep
) -> DeploymentOut:
    if await crud.get_agent(session, data.agent_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    deployment = await crud.create_deployment(session, data)
    return DeploymentOut.model_validate(deployment)


@router.get("", response_model=list[DeploymentOut])
async def list_deployments(
    session: SessionDep, agent_id: uuid.UUID | None = None
) -> list[DeploymentOut]:
    deployments = await crud.list_deployments(session, agent_id)
    return [DeploymentOut.model_validate(d) for d in deployments]


@router.get("/{deployment_id}", response_model=DeploymentOut)
async def get_deployment(
    deployment_id: uuid.UUID, session: SessionDep
) -> DeploymentOut:
    deployment = await crud.get_deployment(session, deployment_id)
    if deployment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "deployment not found")
    return DeploymentOut.model_validate(deployment)
