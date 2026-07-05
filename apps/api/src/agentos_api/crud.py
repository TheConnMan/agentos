"""Database access helpers for agents, versions, and deployments."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Agent, AgentVersion, Deployment
from .schemas import AgentCreate, DeploymentCreate, VersionCreate


async def get_version(
    session: AsyncSession, version_id: uuid.UUID
) -> AgentVersion | None:
    return await session.get(AgentVersion, version_id)


async def attach_bundle(
    session: AsyncSession,
    version: AgentVersion,
    bundle_ref: str,
    bundle_sha256: str,
) -> AgentVersion:
    version.bundle_ref = bundle_ref
    version.bundle_sha256 = bundle_sha256
    await session.commit()
    await session.refresh(version)
    return version


async def create_agent(session: AsyncSession, data: AgentCreate) -> Agent:
    agent = Agent(name=data.name, slack_channel=data.slack_channel)
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def list_agents(session: AsyncSession) -> list[Agent]:
    result = await session.scalars(select(Agent).order_by(Agent.created_at))
    return list(result)


async def get_agent(session: AsyncSession, agent_id: uuid.UUID) -> Agent | None:
    return await session.get(Agent, agent_id)


async def create_version(
    session: AsyncSession, agent_id: uuid.UUID, data: VersionCreate
) -> AgentVersion:
    version = AgentVersion(
        agent_id=agent_id,
        version_label=data.version_label,
        bundle_ref=data.bundle_ref,
        created_by=data.created_by,
    )
    session.add(version)
    await session.commit()
    await session.refresh(version)
    return version


async def list_versions(
    session: AsyncSession, agent_id: uuid.UUID
) -> list[AgentVersion]:
    result = await session.scalars(
        select(AgentVersion)
        .where(AgentVersion.agent_id == agent_id)
        .order_by(AgentVersion.created_at)
    )
    return list(result)


async def create_deployment(
    session: AsyncSession, data: DeploymentCreate
) -> Deployment:
    deployment = Deployment(
        agent_id=data.agent_id,
        version_id=data.version_id,
        environment=data.environment,
        status=data.status,
    )
    session.add(deployment)
    await session.commit()
    await session.refresh(deployment)
    return deployment


async def list_deployments(
    session: AsyncSession, agent_id: uuid.UUID | None = None
) -> list[Deployment]:
    stmt = select(Deployment).order_by(Deployment.deployed_at)
    if agent_id is not None:
        stmt = stmt.where(Deployment.agent_id == agent_id)
    result = await session.scalars(stmt)
    return list(result)


async def get_deployment(
    session: AsyncSession, deployment_id: uuid.UUID
) -> Deployment | None:
    return await session.get(Deployment, deployment_id)
