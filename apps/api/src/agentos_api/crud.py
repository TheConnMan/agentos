"""Database access helpers for agents, versions, and deployments."""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Agent, AgentVersion, Deployment, Environment
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
    agent = Agent(
        name=data.name,
        slack_channel=data.slack_channel,
        repo_full_name=data.repo_full_name,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def list_agents(session: AsyncSession) -> list[Agent]:
    result = await session.scalars(select(Agent).order_by(Agent.created_at))
    return list(result)


async def get_agent(session: AsyncSession, agent_id: uuid.UUID) -> Agent | None:
    return await session.get(Agent, agent_id)


async def agent_has_active_deployment(
    session: AsyncSession, agent_id: uuid.UUID
) -> bool:
    result = await session.scalar(
        select(Deployment.id)
        .where(Deployment.agent_id == agent_id, Deployment.status == "active")
        .limit(1)
    )
    return result is not None


async def delete_agent(session: AsyncSession, agent_id: uuid.UUID) -> None:
    # Remove child rows first, then the agent. Bulk deletes bypass the ORM
    # relationship cascade (which would emit an async lazy-load during flush) and
    # match the FK ondelete=CASCADE already declared on both child tables. Bundle
    # objects in MinIO are intentionally left in place (out of scope).
    await session.execute(
        delete(Deployment).where(Deployment.agent_id == agent_id)
    )
    await session.execute(
        delete(AgentVersion).where(AgentVersion.agent_id == agent_id)
    )
    await session.execute(delete(Agent).where(Agent.id == agent_id))
    await session.commit()


async def update_agent_channel(
    session: AsyncSession, agent: Agent, slack_channel: str
) -> Agent:
    agent.slack_channel = slack_channel
    await session.commit()
    await session.refresh(agent)
    return agent


async def update_budget(
    session: AsyncSession,
    agent: Agent,
    max_usd_per_day: float | None,
    max_output_tokens_per_run: int | None,
) -> Agent:
    agent.max_usd_per_day = max_usd_per_day
    agent.max_output_tokens_per_run = max_output_tokens_per_run
    await session.commit()
    await session.refresh(agent)
    return agent


async def get_agent_by_repo(
    session: AsyncSession, repo_full_name: str
) -> Agent | None:
    agent: Agent | None = await session.scalar(
        select(Agent).where(Agent.repo_full_name == repo_full_name)
    )
    return agent


async def create_version_row(
    session: AsyncSession,
    agent_id: uuid.UUID,
    version_label: str,
    created_by: str,
    commit_sha: str | None = None,
    bundle_ref: str | None = None,
) -> AgentVersion:
    version = AgentVersion(
        agent_id=agent_id,
        version_label=version_label,
        created_by=created_by,
        commit_sha=commit_sha,
        bundle_ref=bundle_ref,
    )
    session.add(version)
    await session.commit()
    await session.refresh(version)
    return version


async def create_version(
    session: AsyncSession, agent_id: uuid.UUID, data: VersionCreate
) -> AgentVersion:
    return await create_version_row(
        session,
        agent_id,
        version_label=data.version_label,
        created_by=data.created_by,
        bundle_ref=data.bundle_ref,
    )


async def get_version_by_commit(
    session: AsyncSession, agent_id: uuid.UUID, commit_sha: str
) -> AgentVersion | None:
    version: AgentVersion | None = await session.scalar(
        select(AgentVersion).where(
            AgentVersion.agent_id == agent_id,
            AgentVersion.commit_sha == commit_sha,
        )
    )
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


async def create_deployment_row(
    session: AsyncSession,
    agent_id: uuid.UUID,
    version_id: uuid.UUID,
    environment: Environment,
    bot_identity: str | None = None,
    commit_sha: str | None = None,
    status: str = "active",
) -> Deployment:
    deployment = Deployment(
        agent_id=agent_id,
        version_id=version_id,
        environment=environment,
        bot_identity=bot_identity,
        commit_sha=commit_sha,
        status=status,
    )
    session.add(deployment)
    await session.commit()
    await session.refresh(deployment)
    return deployment


async def create_deployment(
    session: AsyncSession, data: DeploymentCreate
) -> Deployment:
    return await create_deployment_row(
        session,
        agent_id=data.agent_id,
        version_id=data.version_id,
        environment=data.environment,
        status=data.status,
    )


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
