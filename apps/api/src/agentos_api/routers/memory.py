"""Agent memory API: inspect learned memory and trace it back to its sources.

Memory is a scoped namespace over the durable state store (#264, ADR-0025): the
runner writes an append-only log at namespace ``memory`` key ``log``, each item a
``{content, provenance}`` record. This router is the read side of that log for
operators -- it does NOT invent a second store. #266 adds the learned-from
trace-back: given an entry, resolve the session and source traces it was learned
from (the "how did it learn that?" answer). The edit/delete write side is #267.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from .. import crud
from ..auth import require_api_key
from ..config import get_settings
from ..deps import SessionDep
from ..models import WorkflowStateEntry
from ..schemas import (
    MemoryEntryOut,
    MemoryProvenanceOut,
    MemoryTraceBackOut,
    SourceTraceOut,
)

router = APIRouter(
    prefix="/agents", tags=["memory"], dependencies=[Depends(require_api_key)]
)

# The runner writes memory here (mirrors runner/agentos_runner/memory.py:
# a single log-shaped key inside the reserved ``memory`` namespace).
MEMORY_NAMESPACE = "memory"
MEMORY_LOG_KEY = "log"


async def _load_log(session: SessionDep, agent_id: uuid.UUID) -> list[dict[str, Any]]:
    """Return the raw memory-log records for an agent (404 if the agent is gone).

    An absent log is an empty list -- a fresh agent with no learned memory yet,
    not an error. A non-list stored value is treated as empty rather than a 500;
    the store's own shape guard (#248 append) keeps it a list in practice.
    """
    if await crud.get_agent(session, agent_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    entry: WorkflowStateEntry | None = await session.scalar(
        select(WorkflowStateEntry).where(
            WorkflowStateEntry.agent_id == agent_id,
            WorkflowStateEntry.namespace == MEMORY_NAMESPACE,
            WorkflowStateEntry.key == MEMORY_LOG_KEY,
        )
    )
    if entry is None or not isinstance(entry.value, list):
        return []
    return [item for item in entry.value if isinstance(item, dict) and "content" in item]


def _provenance_of(record: dict[str, Any]) -> dict[str, Any]:
    prov = record.get("provenance")
    return prov if isinstance(prov, dict) else {}


def _trace_url(trace_id: str) -> str:
    """A Langfuse deep link for a source trace id (the trace-back target)."""
    base = get_settings().langfuse_host.rstrip("/")
    return f"{base}/trace/{trace_id}"


@router.get("/{agent_id}/memory", response_model=list[MemoryEntryOut])
async def list_memory(agent_id: uuid.UUID, session: SessionDep) -> list[MemoryEntryOut]:
    """List an agent's learned memory entries, oldest first, with provenance."""
    records = await _load_log(session, agent_id)
    return [
        MemoryEntryOut(
            index=i,
            content=str(rec.get("content", "")),
            provenance=MemoryProvenanceOut(**_provenance_of(rec)),
        )
        for i, rec in enumerate(records)
    ]


@router.get(
    "/{agent_id}/memory/{index}/provenance", response_model=MemoryTraceBackOut
)
async def memory_trace_back(
    agent_id: uuid.UUID, index: int, session: SessionDep
) -> MemoryTraceBackOut:
    """Resolve one entry's learned-from trace-back (#266).

    Returns the session and the source traces the lesson was distilled from,
    each with a Langfuse deep link. Reuses the provenance the runner recorded at
    ``remember`` time -- it does not re-derive or invent provenance.
    """
    records = await _load_log(session, agent_id)
    if index < 0 or index >= len(records):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "memory entry not found")
    record = records[index]
    prov = _provenance_of(record)
    trace_ids = prov.get("source_trace_ids") or []
    return MemoryTraceBackOut(
        index=index,
        content=str(record.get("content", "")),
        learned_from_session_id=prov.get("learned_from_session_id"),
        recorded_at=prov.get("recorded_at", ""),
        source_traces=[
            SourceTraceOut(trace_id=str(tid), trace_url=_trace_url(str(tid)))
            for tid in trace_ids
        ],
    )
