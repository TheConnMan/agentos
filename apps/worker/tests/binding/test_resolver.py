"""BindingResolver against the REAL compose Postgres (never mocked).

Seeds agents / agent_versions / deployments in the agentos schema, then checks
channel resolution, the prod-over-dev preference, unknown-channel -> None, and
the budget/env construction. Rows are namespaced by a per-test token and cleaned
up afterwards.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest
from agentos_worker.binding import (
    AGENT_ID_ENV,
    APPROVAL_REQUIRED_ENV,
    BUDGET_ENV,
    BUNDLE_REF_ENV,
    BindingResolver,
)
from agentos_worker.config import WorkerConfig
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:25432/postgres"
)
_SCHEMA = os.environ.get("TEST_DB_SCHEMA", "agentos")


async def _seed_agent(
    engine: AsyncEngine,
    *,
    channel: str,
    name: str,
    max_usd: float | None,
    max_tokens: int | None,
    approval_tools: list[str] | None = None,
) -> uuid.UUID:
    agent_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"INSERT INTO {_SCHEMA}.agents "
                "(id, name, slack_channel, max_usd_per_day, max_output_tokens_per_run, "
                "approval_required_tools) "
                "VALUES (:id, :name, :channel, :usd, :tokens, "
                "CAST(:approval_tools AS jsonb))"
            ),
            {
                "id": agent_id,
                "name": name,
                "channel": channel,
                "usd": max_usd,
                "tokens": max_tokens,
                "approval_tools": (
                    json.dumps(approval_tools) if approval_tools is not None else None
                ),
            },
        )
    return agent_id


async def _seed_deployment(
    engine: AsyncEngine,
    *,
    agent_id: uuid.UUID,
    environment: str,
    bundle_ref: str,
    status: str = "active",
) -> None:
    version_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"INSERT INTO {_SCHEMA}.agent_versions "
                "(id, agent_id, version_label, bundle_ref, created_by) "
                "VALUES (:id, :agent_id, :label, :ref, :by)"
            ),
            {"id": version_id, "agent_id": agent_id, "label": f"v-{environment}",
             "ref": bundle_ref, "by": "test"},
        )
        await conn.execute(
            text(
                f"INSERT INTO {_SCHEMA}.deployments "
                "(id, agent_id, version_id, environment, status) "
                "VALUES (:id, :agent_id, :version_id, "
                f"CAST(:env AS {_SCHEMA}.environment), :status)"
            ),
            {"id": uuid.uuid4(), "agent_id": agent_id, "version_id": version_id,
             "env": environment, "status": status},
        )


async def _cleanup(engine: AsyncEngine, agent_ids: list[uuid.UUID]) -> None:
    async with engine.begin() as conn:
        for agent_id in agent_ids:
            await conn.execute(
                text(f"DELETE FROM {_SCHEMA}.agents WHERE id = :id"), {"id": agent_id}
            )


def _resolver(engine: AsyncEngine) -> BindingResolver:
    return BindingResolver(engine, WorkerConfig(db_schema=_SCHEMA))


def test_resolves_channel_to_active_deployment_and_builds_env() -> None:
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            try:
                async with engine.connect():
                    pass
            except SQLAlchemyError as exc:
                pytest.skip(f"Postgres not reachable at {_DB_URL}: {exc}")

            token = uuid.uuid4().hex[:8]
            channel = f"C-{token}"
            agent_id = await _seed_agent(
                engine, channel=channel, name=f"agent-{token}", max_usd=3.5, max_tokens=4242
            )
            await _seed_deployment(
                engine, agent_id=agent_id, environment="prod", bundle_ref=f"bundles/{token}.zip"
            )

            resolved = await _resolver(engine).resolve(channel)
            assert resolved is not None
            assert resolved.agent_id == agent_id
            assert resolved.bundle_ref == f"bundles/{token}.zip"
            assert resolved.max_usd_per_day == 3.5
            assert resolved.max_output_tokens_per_run == 4242

            env = _resolver(engine).boot_env(resolved, "thread-1")
            assert env[AGENT_ID_ENV] == str(agent_id)
            assert env[BUNDLE_REF_ENV] == f"bundles/{token}.zip"
            assert '"max_usd_per_day":3.5' in env[BUDGET_ENV]
            assert '"max_output_tokens_per_run":4242' in env[BUDGET_ENV]

            await _cleanup(engine, [agent_id])
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_prod_deployment_wins_over_dev() -> None:
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            try:
                async with engine.connect():
                    pass
            except SQLAlchemyError as exc:
                pytest.skip(f"Postgres not reachable: {exc}")

            token = uuid.uuid4().hex[:8]
            channel = f"C-{token}"
            agent_id = await _seed_agent(
                engine, channel=channel, name=f"agent-{token}", max_usd=None, max_tokens=None
            )
            await _seed_deployment(
                engine, agent_id=agent_id, environment="dev", bundle_ref="bundles/dev.zip"
            )
            await _seed_deployment(
                engine, agent_id=agent_id, environment="prod", bundle_ref="bundles/prod.zip"
            )

            resolved = await _resolver(engine).resolve(channel)
            assert resolved is not None
            assert resolved.bundle_ref == "bundles/prod.zip"  # prod wins

            # NULL budget columns -> platform defaults in the env.
            env = _resolver(engine).boot_env(resolved, "t")
            cfg = WorkerConfig()
            assert f'"max_usd_per_day":{cfg.default_max_usd_per_day}' in env[BUDGET_ENV]

            await _cleanup(engine, [agent_id])
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_multiple_agents_on_one_channel_warns_and_picks_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            try:
                async with engine.connect():
                    pass
            except SQLAlchemyError as exc:
                pytest.skip(f"Postgres not reachable: {exc}")

            token = uuid.uuid4().hex[:8]
            channel = f"C-{token}"
            # Two distinct agents bound to the same channel, both active.
            a_prod = await _seed_agent(
                engine, channel=channel, name=f"agent-a-{token}", max_usd=None, max_tokens=None
            )
            a_dev = await _seed_agent(
                engine, channel=channel, name=f"agent-b-{token}", max_usd=None, max_tokens=None
            )
            await _seed_deployment(
                engine, agent_id=a_prod, environment="prod", bundle_ref="bundles/a.zip"
            )
            await _seed_deployment(
                engine, agent_id=a_dev, environment="dev", bundle_ref="bundles/b.zip"
            )

            with caplog.at_level("WARNING"):
                resolved = await _resolver(engine).resolve(channel)

            # Deterministic winner (prod outranks dev) and a warning naming the
            # shadowed agent, rather than a silent drop (#38).
            assert resolved is not None
            assert resolved.agent_id == a_prod
            assert any(
                "agents bound" in r.message and str(a_dev) in r.message
                for r in caplog.records
            )

            await _cleanup(engine, [a_prod, a_dev])
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_deployment_pointing_at_another_agents_version_does_not_resolve() -> None:
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            try:
                async with engine.connect():
                    pass
            except SQLAlchemyError as exc:
                pytest.skip(f"Postgres not reachable: {exc}")

            token = uuid.uuid4().hex[:8]
            channel = f"C-{token}"
            agent_a = await _seed_agent(
                engine, channel=channel, name=f"a-{token}", max_usd=None, max_tokens=None
            )
            agent_b = await _seed_agent(
                engine, channel=f"C-other-{token}", name=f"b-{token}", max_usd=None, max_tokens=None
            )
            # Give B a version, then point an active deployment for A at B's version.
            b_version = uuid.uuid4()
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        f"INSERT INTO {_SCHEMA}.agent_versions "
                        "(id, agent_id, version_label, bundle_ref, created_by) "
                        "VALUES (:id, :agent_id, 'v', 'bundles/b.zip', 't')"
                    ),
                    {"id": b_version, "agent_id": agent_b},
                )
                await conn.execute(
                    text(
                        f"INSERT INTO {_SCHEMA}.deployments "
                        "(id, agent_id, version_id, environment, status) VALUES "
                        f"(:id, :agent_id, :vid, CAST('prod' AS {_SCHEMA}.environment), 'active')"
                    ),
                    {"id": uuid.uuid4(), "agent_id": agent_a, "vid": b_version},
                )

            # The agent-scoped join refuses B's bundle for A's channel.
            resolved = await _resolver(engine).resolve(channel)
            assert resolved is None

            await _cleanup(engine, [agent_a, agent_b])
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_behavior_packs_round_trip_and_parse() -> None:
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            try:
                async with engine.connect():
                    pass
            except SQLAlchemyError as exc:
                pytest.skip(f"Postgres not reachable: {exc}")

            token = uuid.uuid4().hex[:8]
            channel = f"C-{token}"
            agent_id = await _seed_agent(
                engine, channel=channel, name=f"agent-{token}", max_usd=None, max_tokens=None
            )
            packs_json = (
                '{"greeting": {"enabled": true, "phrases": ["hi"], "reply": "yo"}, '
                '"load": {"enabled": true, "lines": ["Working..."]}}'
            )
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        f"UPDATE {_SCHEMA}.agents SET behavior_packs = CAST(:p AS jsonb) "
                        "WHERE id = :id"
                    ),
                    {"p": packs_json, "id": agent_id},
                )
            await _seed_deployment(
                engine, agent_id=agent_id, environment="prod", bundle_ref="bundles/x.zip"
            )

            resolved = await _resolver(engine).resolve(channel)
            assert resolved is not None
            packs = _resolver(engine).packs_for(resolved)
            assert packs.greeting.enabled is True
            assert packs.greeting.reply == "yo"

            await _cleanup(engine, [agent_id])
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_no_packs_parses_to_all_off_default() -> None:
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            try:
                async with engine.connect():
                    pass
            except SQLAlchemyError as exc:
                pytest.skip(f"Postgres not reachable: {exc}")

            token = uuid.uuid4().hex[:8]
            channel = f"C-{token}"
            agent_id = await _seed_agent(
                engine, channel=channel, name=f"agent-{token}", max_usd=None, max_tokens=None
            )
            await _seed_deployment(
                engine, agent_id=agent_id, environment="dev", bundle_ref="bundles/x.zip"
            )
            resolved = await _resolver(engine).resolve(channel)
            assert resolved is not None
            assert resolved.behavior_packs is None
            packs = _resolver(engine).packs_for(resolved)
            assert packs.greeting.enabled is False
            assert packs.tips.enabled is False

            await _cleanup(engine, [agent_id])
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_unknown_channel_resolves_to_none() -> None:
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            try:
                async with engine.connect():
                    pass
            except SQLAlchemyError as exc:
                pytest.skip(f"Postgres not reachable: {exc}")
            resolved = await _resolver(engine).resolve(f"C-nonexistent-{uuid.uuid4().hex}")
            assert resolved is None
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_resolves_approval_required_tools_into_boot_env() -> None:
    # Permission gates (#245): a gated agent's tool list survives the SQL
    # resolve (JSONB decode included) and lands comma-joined in the boot env.
    async def go() -> None:
        engine = create_async_engine(_DB_URL)
        try:
            try:
                async with engine.connect():
                    pass
            except SQLAlchemyError as exc:
                pytest.skip(f"Postgres not reachable at {_DB_URL}: {exc}")

            token = uuid.uuid4().hex[:8]
            channel = f"C-{token}"
            agent_id = await _seed_agent(
                engine,
                channel=channel,
                name=f"agent-{token}",
                max_usd=None,
                max_tokens=None,
                approval_tools=["Bash", "mcp__github__create_issue"],
            )
            await _seed_deployment(
                engine, agent_id=agent_id, environment="prod", bundle_ref=f"bundles/{token}.zip"
            )
            try:
                resolved = await _resolver(engine).resolve(channel)
                assert resolved is not None
                assert resolved.approval_required_tools == [
                    "Bash",
                    "mcp__github__create_issue",
                ]
                env = _resolver(engine).boot_env(resolved, "thread-1")
                assert env[APPROVAL_REQUIRED_ENV] == "Bash,mcp__github__create_issue"
            finally:
                await _cleanup(engine, [agent_id])
        finally:
            await engine.dispose()

    asyncio.run(go())
