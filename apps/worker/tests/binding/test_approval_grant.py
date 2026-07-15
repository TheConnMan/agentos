"""BindingResolver.approval_grant_tool against the REAL compose Postgres (#430,
ADR-0035): the server-side derivation of the one-shot post-approval grant.

The grant is the approved tool name, recovered from the durable ``approvals``
row keyed by the deterministic resume event id. It is delivered ONLY for a
genuinely ``approved`` PERMISSION-GATE approval (summary carries the
``summarize_tool_call`` prefix); a policy-gate approval, a non-approved status,
or a non-approval event id all yield None.

Integration-style: the approvals row is INSERTed against the same async engine
and schema the other binding tests use, never mocked. Two pinning tests import
the source helpers (``resume_event_id``, ``summarize_tool_call``) so a format
change in either producer fails CI rather than silently disabling the grant.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from agentos_api.resumequeue import resume_event_id
from agentos_runner.approval import summarize_tool_call
from agentos_worker.binding import BindingResolver
from agentos_worker.config import WorkerConfig
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:25432/postgres"
)
_SCHEMA = os.environ.get("TEST_DB_SCHEMA", "agentos")


def _resolver(engine: AsyncEngine) -> BindingResolver:
    return BindingResolver(engine, WorkerConfig(db_schema=_SCHEMA))


async def _seed_agent(engine: AsyncEngine, agent_id: uuid.UUID) -> None:
    # The approvals.agent_id FK references agents.id, so a non-NULL grant-bound
    # approval needs a real agent row. Minimal columns only (id/name/channel).
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"INSERT INTO {_SCHEMA}.agents (id, name, slack_channel) "
                "VALUES (:id, :name, :channel)"
            ),
            {"id": agent_id, "name": f"agent-{agent_id.hex[:8]}", "channel": "C1"},
        )


async def _seed_approval(
    engine: AsyncEngine,
    *,
    approval_id: uuid.UUID,
    status: str,
    summary: str,
    agent_id: uuid.UUID | None = None,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"INSERT INTO {_SCHEMA}.approvals "
                "(id, agent_id, conversation_id, author, summary, reply_channel, "
                "reply_placeholder, dedupe_key, status) "
                "VALUES (:id, :agent_id, :conv, :author, :summary, :ch, :ph, "
                ":dedupe, :status)"
            ),
            {
                "id": approval_id,
                "agent_id": agent_id,
                "conv": f"th-{approval_id.hex[:8]}",
                "author": "U1",
                "summary": summary,
                "ch": "C1",
                "ph": "p-1",
                "dedupe": uuid.uuid4().hex,
                "status": status,
            },
        )


async def _cleanup_approvals(engine: AsyncEngine, ids: list[uuid.UUID]) -> None:
    async with engine.begin() as conn:
        for approval_id in ids:
            await conn.execute(
                text(f"DELETE FROM {_SCHEMA}.approvals WHERE id = :id"), {"id": approval_id}
            )


async def _cleanup_agents(engine: AsyncEngine, ids: list[uuid.UUID]) -> None:
    # Deleting the agent CASCADEs to any approvals still bound to it.
    async with engine.begin() as conn:
        for agent_id in ids:
            await conn.execute(
                text(f"DELETE FROM {_SCHEMA}.agents WHERE id = :id"), {"id": agent_id}
            )


async def _skip_if_unreachable(engine: AsyncEngine) -> None:
    try:
        async with engine.connect():
            pass
    except SQLAlchemyError as exc:
        pytest.skip(f"Postgres not reachable at {_DB_URL}: {exc}")


def test_returns_tool_for_approved_permission_gate() -> None:
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            await _skip_if_unreachable(engine)
            agent_id = uuid.uuid4()
            approval_id = uuid.uuid4()
            summary = summarize_tool_call(
                "mcp__github__create_issue", {"title": "Ship the fix"}
            )
            await _seed_agent(engine, agent_id)
            await _seed_approval(
                engine,
                approval_id=approval_id,
                status="approved",
                summary=summary,
                agent_id=agent_id,
            )
            try:
                tool = await _resolver(engine).approval_grant_tool(
                    resume_event_id(approval_id), agent_id
                )
                assert tool == "mcp__github__create_issue"
            finally:
                await _cleanup_agents(engine, [agent_id])
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_returns_none_for_non_approved_statuses() -> None:
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            await _skip_if_unreachable(engine)
            summary = summarize_tool_call("Bash", {"command": "deploy"})
            agent_id = uuid.uuid4()
            await _seed_agent(engine, agent_id)
            try:
                for status in ("rejected", "expired", "pending"):
                    approval_id = uuid.uuid4()
                    await _seed_approval(
                        engine,
                        approval_id=approval_id,
                        status=status,
                        summary=summary,
                        agent_id=agent_id,
                    )
                    tool = await _resolver(engine).approval_grant_tool(
                        resume_event_id(approval_id), agent_id
                    )
                    assert tool is None, f"status={status} must not grant"
            finally:
                # Deleting the agent CASCADEs to its bound approvals.
                await _cleanup_agents(engine, [agent_id])
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_returns_none_for_approved_policy_gate_without_prefix() -> None:
    # The security guard: an APPROVED policy-gate approval (request_approval, a
    # business decision) has an arbitrary summary lacking the permission-gate
    # prefix, so it must NOT hand the model a grant that bypasses any gated tool.
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            await _skip_if_unreachable(engine)
            agent_id = uuid.uuid4()
            approval_id = uuid.uuid4()
            await _seed_agent(engine, agent_id)
            await _seed_approval(
                engine,
                approval_id=approval_id,
                status="approved",
                summary="Give ACME a 20% discount",  # policy-gate: no prefix
                agent_id=agent_id,
            )
            try:
                # Passing the matching agent id proves the None is the prefix
                # guard firing, not the agent-bind guard short-circuiting.
                tool = await _resolver(engine).approval_grant_tool(
                    resume_event_id(approval_id), agent_id
                )
                assert tool is None
            finally:
                await _cleanup_agents(engine, [agent_id])
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_grant_is_bound_to_the_approvals_agent() -> None:
    # The cross-agent guard (#430): a genuinely approved permission-gate grant
    # is delivered ONLY to the agent the approval belongs to. A resolver call
    # for a DIFFERENT agent (e.g. the channel was rebound while pending) must
    # return None rather than cross-authorize a shared gated tool name.
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            await _skip_if_unreachable(engine)
            owner_id = uuid.uuid4()
            other_id = uuid.uuid4()
            approval_id = uuid.uuid4()
            summary = summarize_tool_call("mcp__github__create_issue", {"n": 1})
            await _seed_agent(engine, owner_id)
            await _seed_approval(
                engine,
                approval_id=approval_id,
                status="approved",
                summary=summary,
                agent_id=owner_id,
            )
            try:
                resolver = _resolver(engine)
                event = resume_event_id(approval_id)
                # Mismatch: a different resolved agent gets nothing.
                assert await resolver.approval_grant_tool(event, other_id) is None
                # Match: the owning agent gets the grant.
                assert (
                    await resolver.approval_grant_tool(event, owner_id)
                    == "mcp__github__create_issue"
                )
            finally:
                await _cleanup_agents(engine, [owner_id])
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_grant_none_when_approval_agent_id_is_null() -> None:
    # Fail-safe: an approval row with a NULL agent_id (a run without a
    # deployment binding) can never hand out a grant, even to any agent.
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            await _skip_if_unreachable(engine)
            approval_id = uuid.uuid4()
            summary = summarize_tool_call("mcp__github__create_issue", {"n": 1})
            await _seed_approval(
                engine,
                approval_id=approval_id,
                status="approved",
                summary=summary,
                agent_id=None,
            )
            try:
                tool = await _resolver(engine).approval_grant_tool(
                    resume_event_id(approval_id), uuid.uuid4()
                )
                assert tool is None
            finally:
                await _cleanup_approvals(engine, [approval_id])
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_returns_none_without_db_hit_for_non_approval_event_id() -> None:
    # A non-approval event id (e.g. a Slack event id) must fast-return None
    # WITHOUT a DB round-trip. Proven by pointing the resolver at an unreachable
    # engine: if the method touched the DB it would raise instead of returning None.
    async def go() -> None:
        bad_engine = create_async_engine(
            "postgresql+asyncpg://invalid:invalid@127.0.0.1:1/none"
        )
        try:
            resolver = BindingResolver(bad_engine, WorkerConfig(db_schema=_SCHEMA))
            tool = await resolver.approval_grant_tool(
                "ev-slack-1699999999.123456", uuid.uuid4()
            )
            assert tool is None
        finally:
            await bad_engine.dispose()

    asyncio.run(go())


def test_pins_resume_event_id_format() -> None:
    # Pinning: the worker parser recovers the approval id from the event id the
    # API's own helper emits. Guards event_id format divergence.
    approval_id = uuid.uuid4()
    assert resume_event_id(approval_id) == f"approval-{approval_id}-resolved"

    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            await _skip_if_unreachable(engine)
            agent_id = uuid.uuid4()
            summary = summarize_tool_call("mcp__github__create_issue", {"n": 1})
            await _seed_agent(engine, agent_id)
            await _seed_approval(
                engine,
                approval_id=approval_id,
                status="approved",
                summary=summary,
                agent_id=agent_id,
            )
            try:
                tool = await _resolver(engine).approval_grant_tool(
                    resume_event_id(approval_id), agent_id
                )
                assert tool == "mcp__github__create_issue"
            finally:
                await _cleanup_agents(engine, [agent_id])
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_pins_summarize_tool_call_format() -> None:
    # Pinning: the worker summary-parser recovers exactly the tool name that
    # summarize_tool_call (the runner producer) writes into the summary. Guards
    # summary format divergence.
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            await _skip_if_unreachable(engine)
            agent_id = uuid.uuid4()
            approval_id = uuid.uuid4()
            summary = summarize_tool_call(
                "mcp__crm__send_contract", {"account": "ACME", "amount": 500}
            )
            await _seed_agent(engine, agent_id)
            await _seed_approval(
                engine,
                approval_id=approval_id,
                status="approved",
                summary=summary,
                agent_id=agent_id,
            )
            try:
                tool = await _resolver(engine).approval_grant_tool(
                    resume_event_id(approval_id), agent_id
                )
                assert tool == "mcp__crm__send_contract"
            finally:
                await _cleanup_agents(engine, [agent_id])
        finally:
            await engine.dispose()

    asyncio.run(go())
