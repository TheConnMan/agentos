"""Durable agent-scoped key/value state (#23, #248).

A small compare-and-set KV/document store: namespace + key per agent, an
arbitrary-JSON value, Postgres JSONB backing. Operations: get / put-with-CAS /
list / delete / append. Two hard non-goals keep this from becoming a database
product: there is no query language (get-by-key + list-by-namespace only), and
both a single value and a whole namespace are size-capped (#248). This is the
API surface the approvals epic (#22) and other cross-turn workflow state
consume; exposing it to bundle code via an auto-mounted MCP server is a later
slice.
"""

import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import crud, sandbox_token
from ..auth import verify_platform_key
from ..config import get_settings
from ..deps import SessionDep
from ..models import WorkflowStateEntry
from ..schemas import StateAppendIn, StateEntryOut, StateEntryPut, StateNamespaceOut


async def require_state_access(
    agent_id: uuid.UUID,
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    """State-router auth (ADR-0033): the platform key (trusted callers) OR a
    scoped ``state`` token bound to this path's ``agent_id`` (the sandbox). Every
    other router keeps the platform-key-only ``require_api_key``."""

    if verify_platform_key(x_api_key):
        return
    if x_api_key is not None and sandbox_token.verify(
        x_api_key, get_settings().api_key, agent=str(agent_id), scope="state"
    ):
        return
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail="missing or invalid credential"
    )


router = APIRouter(
    prefix="/agents", tags=["state"], dependencies=[Depends(require_state_access)]
)


def _json_size(value: Any) -> int:
    """Serialized-JSON byte length, the unit both size caps are measured in."""
    return len(json.dumps(value, separators=(",", ":")).encode("utf-8"))


async def _enforce_caps(
    session: AsyncSession,
    agent_id: uuid.UUID,
    namespace: str,
    key: str,
    value: Any,
) -> None:
    """Reject a write that breaks the per-value or per-namespace size cap (#248).

    The namespace total counts the incoming value plus every *other* key already
    in the namespace (the key being written replaces its own prior size).
    """
    settings = get_settings()
    value_bytes = _json_size(value)
    if value_bytes > settings.state_max_value_bytes:
        raise HTTPException(
            413,
            f"value is {value_bytes} bytes, over the "
            f"{settings.state_max_value_bytes}-byte per-value cap",
        )

    others = await session.scalars(
        select(WorkflowStateEntry.value).where(
            WorkflowStateEntry.agent_id == agent_id,
            WorkflowStateEntry.namespace == namespace,
            WorkflowStateEntry.key != key,
        )
    )
    namespace_bytes = value_bytes + sum(_json_size(v) for v in others)
    if namespace_bytes > settings.state_max_namespace_bytes:
        raise HTTPException(
            413,
            f"namespace {namespace!r} would be {namespace_bytes} bytes, over the "
            f"{settings.state_max_namespace_bytes}-byte per-namespace cap",
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
    await _enforce_caps(session, agent_id, namespace, key, data.value)
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


@router.post(
    "/{agent_id}/state/{namespace}/{key}/append", response_model=StateEntryOut
)
async def append_state(
    agent_id: uuid.UUID,
    namespace: str,
    key: str,
    data: StateAppendIn,
    session: SessionDep,
) -> StateEntryOut:
    """Append an item to a log-shaped (JSON array) entry (#248).

    Creates the entry as a single-element array if absent; otherwise the stored
    value must already be an array, else the append is a 409. Subject to the
    same per-value and per-namespace size caps as a put.
    """
    if await crud.get_agent(session, agent_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    entry: WorkflowStateEntry | None = await session.scalar(
        select(WorkflowStateEntry)
        .where(
            WorkflowStateEntry.agent_id == agent_id,
            WorkflowStateEntry.namespace == namespace,
            WorkflowStateEntry.key == key,
        )
        .with_for_update()
    )
    if entry is None:
        new_value = [data.item]
        await _enforce_caps(session, agent_id, namespace, key, new_value)
        entry = WorkflowStateEntry(
            agent_id=agent_id, namespace=namespace, key=key, value=new_value
        )
        session.add(entry)
    else:
        if not isinstance(entry.value, list):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "cannot append: stored value is not a JSON array",
            )
        new_value = [*entry.value, data.item]
        await _enforce_caps(session, agent_id, namespace, key, new_value)
        entry.value = new_value
        entry.version += 1
    await session.commit()
    await session.refresh(entry)
    return StateEntryOut.model_validate(entry)


@router.get("/{agent_id}/state", response_model=list[StateNamespaceOut])
async def list_namespaces(
    agent_id: uuid.UUID, session: SessionDep
) -> list[StateNamespaceOut]:
    """List the namespaces an agent has stored, each with its key count and the
    most recent write time (#250). This is the enumeration the operator's
    read/inspect surface needs on top of get-by-key + list-by-namespace; it stays
    within the store's non-goals (no query language, just a grouped summary).
    Namespaces are returned most-recently-written first.
    """
    rows = await session.execute(
        select(
            WorkflowStateEntry.namespace,
            func.count().label("key_count"),
            func.max(WorkflowStateEntry.updated_at).label("last_updated"),
        )
        .where(WorkflowStateEntry.agent_id == agent_id)
        .group_by(WorkflowStateEntry.namespace)
        .order_by(func.max(WorkflowStateEntry.updated_at).desc())
    )
    return [
        StateNamespaceOut(
            namespace=row.namespace,
            key_count=row.key_count,
            last_updated=row.last_updated,
        )
        for row in rows
    ]


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
