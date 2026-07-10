"""Durable agent-scoped key/value state (#23, first slice).

A small compare-and-set KV store: namespace + key per agent, an arbitrary-JSON
value, Postgres JSONB backing. This is the API surface the approvals epic (#22)
and other cross-turn workflow state will consume; exposing it to bundle code via
an auto-mounted MCP server is a later slice.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import crud
from ..auth import require_api_key
from ..deps import SessionDep
from ..models import WorkflowStateEntry
from ..schemas import StateEntryOut, StateEntryPut

router = APIRouter(
    prefix="/agents", tags=["state"], dependencies=[Depends(require_api_key)]
)


async def _get_entry(
    session: AsyncSession, agent_id: uuid.UUID, namespace: str, key: str
) -> WorkflowStateEntry | None:
    entry: WorkflowStateEntry | None = await session.scalar(
        select(WorkflowStateEntry).where(
            WorkflowStateEntry.agent_id == agent_id,
            WorkflowStateEntry.namespace == namespace,
            WorkflowStateEntry.key == key,
        )
    )
    return entry


@router.put("/{agent_id}/state/{namespace}/{key}", response_model=StateEntryOut)
async def put_state(
    agent_id: uuid.UUID,
    namespace: str,
    key: str,
    data: StateEntryPut,
    session: SessionDep,
) -> StateEntryOut:
    # Unknown agent is a 404 (the FK would also reject, but this is the clear
    # signal). expected_version opts into compare-and-set.
    if await crud.get_agent(session, agent_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    entry = await _get_entry(session, agent_id, namespace, key)
    if entry is None:
        if data.expected_version is not None:
            # A CAS put that expects a prior version cannot create the entry.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "version mismatch: entry does not exist yet",
            )
        entry = WorkflowStateEntry(
            agent_id=agent_id, namespace=namespace, key=key, value=data.value
        )
        session.add(entry)
    else:
        if data.expected_version is not None and data.expected_version != entry.version:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"version mismatch: expected {data.expected_version}, "
                f"stored {entry.version}",
            )
        entry.value = data.value
        entry.version += 1
    await session.commit()
    await session.refresh(entry)
    return StateEntryOut.model_validate(entry)


@router.get("/{agent_id}/state/{namespace}/{key}", response_model=StateEntryOut)
async def get_state(
    agent_id: uuid.UUID, namespace: str, key: str, session: SessionDep
) -> StateEntryOut:
    entry = await _get_entry(session, agent_id, namespace, key)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "state entry not found")
    return StateEntryOut.model_validate(entry)


@router.get("/{agent_id}/state/{namespace}", response_model=list[StateEntryOut])
async def list_state(
    agent_id: uuid.UUID, namespace: str, session: SessionDep
) -> list[StateEntryOut]:
    entries = await session.scalars(
        select(WorkflowStateEntry)
        .where(
            WorkflowStateEntry.agent_id == agent_id,
            WorkflowStateEntry.namespace == namespace,
        )
        .order_by(WorkflowStateEntry.key)
    )
    return [StateEntryOut.model_validate(e) for e in entries]


@router.delete(
    "/{agent_id}/state/{namespace}/{key}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_state(
    agent_id: uuid.UUID, namespace: str, key: str, session: SessionDep
) -> Response:
    entry = await _get_entry(session, agent_id, namespace, key)
    if entry is not None:
        await session.delete(entry)
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
