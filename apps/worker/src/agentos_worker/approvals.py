"""Worker-side access to the durable ``approvals`` table (#22, ADR-0010).

The API owns the table's schema and migration and the resolve-once compare-and-set
endpoint; the worker writes the *pending* record when a session pauses on a gate
and marks it *resumed* once the resumed turn is enqueued. Like ``binding.py`` this
is a thin raw-SQL layer over the same shared Postgres via the worker's async
engine, deliberately not an import of the API package (which would pull FastAPI
and the ORM into the worker). The column set here must track the API migration.

Two writers, one table, by design: the worker only INSERTs pending rows and sets
``resumed_at``; the pending -> approved/rejected transition is the API's atomic
CAS. They never contend on the same column.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .config import WorkerConfig


class ApprovalRecord(BaseModel):
    """A durable approval row, as the resumer needs it to rebuild the turn."""

    id: uuid.UUID
    agent_id: uuid.UUID
    conversation_id: str
    session_id: str | None
    channel: str
    reply_placeholder: str
    reply_endpoint: str | None
    tool: str
    prompt: str
    status: str


_INSERT_PENDING = """
INSERT INTO {schema}.approvals (
    id, agent_id, conversation_id, session_id, channel, reply_placeholder,
    reply_endpoint, tool, tool_use_id, input_digest, prompt, requested_by
) VALUES (
    :id, :agent_id, :conversation_id, :session_id, :channel, :reply_placeholder,
    :reply_endpoint, :tool, :tool_use_id, :input_digest, :prompt, :requested_by
)
ON CONFLICT (agent_id, conversation_id, tool_use_id) DO NOTHING
RETURNING id
"""

_SELECT_EXISTING_ID = """
SELECT id FROM {schema}.approvals
WHERE agent_id = :agent_id AND conversation_id = :conversation_id
  AND tool_use_id = :tool_use_id
"""

_SELECT_RESOLVED_UNRESUMED = """
SELECT id, agent_id, conversation_id, session_id, channel, reply_placeholder,
       reply_endpoint, tool, prompt, status
FROM {schema}.approvals
WHERE status IN ('approved', 'rejected') AND resumed_at IS NULL
ORDER BY resolved_at
"""

_SELECT_ONE = """
SELECT id, agent_id, conversation_id, session_id, channel, reply_placeholder,
       reply_endpoint, tool, prompt, status
FROM {schema}.approvals
WHERE id = :id
"""

_MARK_RESUMED = """
UPDATE {schema}.approvals SET resumed_at = now()
WHERE id = :id AND resumed_at IS NULL
"""


class ApprovalStore:
    """Create pending approvals and mark them resumed (raw SQL, shared engine)."""

    def __init__(self, engine: AsyncEngine, config: WorkerConfig) -> None:
        self._engine = engine
        schema = config.db_schema
        self._insert = text(_INSERT_PENDING.format(schema=schema))
        self._existing = text(_SELECT_EXISTING_ID.format(schema=schema))
        self._resolved = text(_SELECT_RESOLVED_UNRESUMED.format(schema=schema))
        self._one = text(_SELECT_ONE.format(schema=schema))
        self._mark_resumed = text(_MARK_RESUMED.format(schema=schema))

    async def create_pending(
        self,
        *,
        agent_id: uuid.UUID,
        conversation_id: str,
        session_id: str | None,
        channel: str,
        reply_placeholder: str,
        reply_endpoint: str | None,
        tool: str,
        tool_use_id: str,
        input_digest: str,
        prompt: str,
        requested_by: str,
    ) -> uuid.UUID:
        """Insert a pending approval, or return the existing id on a re-suspend.

        Idempotent on ``(agent_id, conversation_id, tool_use_id)``: a reclaimed
        turn that re-emits the same gate does not create a second record, so the
        suspend/resume cycle stays single-writer safe.
        """
        params = {
            "id": uuid.uuid4(),
            "agent_id": agent_id,
            "conversation_id": conversation_id,
            "session_id": session_id,
            "channel": channel,
            "reply_placeholder": reply_placeholder,
            "reply_endpoint": reply_endpoint,
            "tool": tool,
            "tool_use_id": tool_use_id,
            "input_digest": input_digest,
            "prompt": prompt,
            "requested_by": requested_by,
        }
        async with self._engine.begin() as conn:
            result = await conn.execute(self._insert, params)
            row = result.first()
            if row is not None:
                return uuid.UUID(str(row[0]))
            # Conflict: the row already exists (a reclaim re-ran the turn).
            existing = await conn.execute(
                self._existing,
                {
                    "agent_id": agent_id,
                    "conversation_id": conversation_id,
                    "tool_use_id": tool_use_id,
                },
            )
            return uuid.UUID(str(existing.scalar_one()))

    async def get(self, approval_id: uuid.UUID) -> ApprovalRecord | None:
        async with self._engine.connect() as conn:
            result = await conn.execute(self._one, {"id": approval_id})
            row = result.mappings().first()
        return ApprovalRecord.model_validate(dict(row)) if row is not None else None

    async def list_resolved_unresumed(self) -> list[ApprovalRecord]:
        """Resolved approvals whose session has not been resumed yet.

        The startup reconcile sweep reads this to catch resolutions that happened
        while every worker was down (the pubsub wake-up reached no subscriber)."""
        async with self._engine.connect() as conn:
            result = await conn.execute(self._resolved)
            rows = result.mappings().all()
        return [ApprovalRecord.model_validate(dict(r)) for r in rows]

    async def mark_resumed(self, approval_id: uuid.UUID) -> bool:
        """Claim the resume for this approval. Returns True for the winner.

        The ``resumed_at IS NULL`` guard makes this a compare-and-set, so a pubsub
        wake-up racing the reconcile sweep enqueues the resume turn exactly once.
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(self._mark_resumed, {"id": approval_id})
        return bool(result.rowcount)
