"""Deployment-to-runtime binding: resolve a Slack channel to the agent, its
active deployment, and the bundle + budget to boot the sandbox with.

The B1/J1/L1 Postgres tables are the source of truth. Rather than import the API
package (which would pull FastAPI and its ORM into the worker), this is a thin
read-only query layer over the same tables via a SQLAlchemy async engine: one
parameterized SELECT joining agents -> deployments -> agent_versions.

Resolution rule: an agent is bound to a channel (agents.slack_channel). The run
uses that agent's active deployment (deployments.status = 'active'); when both a
prod and a dev deployment are active, prod wins, then the most recent. A channel
with no agent, or an agent with no active deployment, resolves to None -- the
kernel answers with a polite placeholder and drops the event rather than crashing.
Per-channel dev/prod bot-identity routing (the dispatcher carrying which bot was
addressed) is a J1/dispatcher refinement noted for later.

Contract (cross-lane, load-bearing): agents.slack_channel MUST store the Slack
channel ID (e.g. ``C0123ABCD``), because the dispatcher enqueues
``QueuedSlackEvent.channel`` as the Slack event's channel id, and this resolver
matches on equality. If the create-agent API/UI stores a channel NAME (``#triage``)
instead, every real mention resolves to None and is dropped. Storing the id at
agent creation (or translating name->id there) is the API/UI's responsibility;
this resolver deliberately does not call the Slack API to translate, to avoid
coupling the worker to a Slack token.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from aci_protocol import Budget
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .behaviorpacks import BehaviorPacks
from .config import WorkerConfig

# Env vars the worker injects into a bound sandbox claim. AGENTOS_BUNDLE_REF is
# the MinIO object key sandbox provisioning fetches into AGENTOS_PLUGIN_DIR (a
# runner/chart handoff); the rest are the frozen ACI SessionConfig env.
BUNDLE_REF_ENV = "AGENTOS_BUNDLE_REF"
PLUGIN_DIR_ENV = "AGENTOS_PLUGIN_DIR"
BUDGET_ENV = "AGENTOS_BUDGET"
SESSION_ID_ENV = "AGENTOS_SESSION_ID"
AGENT_ID_ENV = "AGENTOS_AGENT_ID"
FAKE_MODEL_ENV = "AGENTOS_FAKE_MODEL"
CREDENTIALS_ENV = "AGENTOS_CREDENTIALS"

_RESOLVE_SQL = """
SELECT a.id AS agent_id,
       a.max_usd_per_day AS max_usd_per_day,
       a.max_output_tokens_per_run AS max_output_tokens_per_run,
       a.behavior_packs AS behavior_packs,
       v.id AS version_id,
       v.version_label AS version_label,
       v.bundle_ref AS bundle_ref
FROM {schema}.agents a
JOIN {schema}.deployments d ON d.agent_id = a.id AND d.status = 'active'
JOIN {schema}.agent_versions v ON v.id = d.version_id AND v.agent_id = a.id
WHERE a.slack_channel = :channel
ORDER BY (d.environment = 'prod') DESC, d.deployed_at DESC
LIMIT 1
"""


class ResolvedDeployment(BaseModel):
    """The agent binding for a channel: which version to run and its budget."""

    agent_id: uuid.UUID
    version_id: uuid.UUID
    version_label: str
    bundle_ref: str | None
    max_usd_per_day: float | None
    max_output_tokens_per_run: int | None
    # The agent's opt-in behavior packs (declarative JSON), or None for the
    # all-off platform default. Parsed into a BehaviorPacks via packs_for().
    behavior_packs: dict[str, Any] | None = None


class BindingResolver:
    """Resolves a Slack channel to its active agent deployment (read-only)."""

    def __init__(self, engine: AsyncEngine, config: WorkerConfig) -> None:
        self._engine = engine
        self._config = config
        # Table identifiers are not user input; the schema comes from config.
        self._sql = text(_RESOLVE_SQL.format(schema=config.db_schema))

    async def resolve(self, channel: str) -> ResolvedDeployment | None:
        async with self._engine.connect() as conn:
            result = await conn.execute(self._sql, {"channel": channel})
            row = result.mappings().first()
        if row is None:
            return None
        data = dict(row)
        # asyncpg returns JSONB as a str for a raw-text SELECT (no column type to
        # trigger SQLAlchemy's json deserializer); decode it to the dict the model
        # expects. A dict (or None) passes through untouched.
        packs = data.get("behavior_packs")
        if isinstance(packs, str):
            data["behavior_packs"] = json.loads(packs)
        return ResolvedDeployment.model_validate(data)

    async def repo_full_name(self, agent_id: uuid.UUID) -> str | None:
        """The agent's GitHub repo (owner/name), for the eval PR-check report."""
        sql = text(f"SELECT repo_full_name FROM {self._config.db_schema}.agents WHERE id = :id")
        async with self._engine.connect() as conn:
            result = await conn.execute(sql, {"id": agent_id})
            row = result.first()
        if row is None:
            return None
        value: str | None = row[0]
        return value

    def packs_for(self, resolved: ResolvedDeployment) -> BehaviorPacks:
        """The agent's parsed behavior packs (all-off when none are configured).

        The kernel wiring that samples a working line / short-circuits a greeting
        consumes this; it is a separate, F1-reviewed change (docs/behavior-packs.md).
        """
        return BehaviorPacks.from_config(resolved.behavior_packs)

    def budget_for(self, resolved: ResolvedDeployment) -> Budget:
        """The AGENTOS_BUDGET for the agent, applying platform defaults for NULLs."""
        return Budget(
            max_output_tokens_per_run=(
                resolved.max_output_tokens_per_run
                if resolved.max_output_tokens_per_run is not None
                else self._config.default_max_output_tokens_per_run
            ),
            max_usd_per_day=(
                resolved.max_usd_per_day
                if resolved.max_usd_per_day is not None
                else self._config.default_max_usd_per_day
            ),
        )

    def boot_env(self, resolved: ResolvedDeployment, thread_key: str) -> dict[str, str]:
        """The env injected into the sandbox claim for a bound run."""
        env = {
            BUDGET_ENV: self.budget_for(resolved).model_dump_json(),
            SESSION_ID_ENV: f"agent-{resolved.agent_id}-thread-{thread_key}",
            AGENT_ID_ENV: str(resolved.agent_id),
            PLUGIN_DIR_ENV: self._config.bundle_plugin_dir,
        }
        if resolved.bundle_ref is not None:
            env[BUNDLE_REF_ENV] = resolved.bundle_ref
        apply_model_env(env, self._config)
        return env


def apply_model_env(env: dict[str, str], config: WorkerConfig) -> None:
    """Layer the runner model + credentials passthrough onto a boot env.

    Shared by the runs binding and the eval consumer so both lanes boot the
    runner the same way: fake_model gates the canned model (no credential
    needed); credentials is forwarded only when set and never logged.
    """
    if config.fake_model:
        env[FAKE_MODEL_ENV] = "1"
    if config.credentials:
        env[CREDENTIALS_ENV] = config.credentials
