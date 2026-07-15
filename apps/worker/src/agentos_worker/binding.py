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
channel ID (e.g. ``C0123ABCD``), because the dispatcher enqueues the Slack
channel id as ``QueuedTurn.reply_handle.channel``, the kernel passes that value
into ``resolve()``, and this resolver matches on equality. If the create-agent
API/UI stores a channel NAME (``#triage``) instead, every real mention resolves
to None and is dropped. Storing the id at
agent creation (or translating name->id there) is the API/UI's responsibility;
this resolver deliberately does not call the Slack API to translate, to avoid
coupling the worker to a Slack token.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
import uuid
from typing import Any
from urllib.parse import quote

from aci_protocol import Budget
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from . import sandbox_token
from .behaviorpacks import BehaviorPacks
from .config import WorkerConfig

logger = logging.getLogger(__name__)

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
# The memory port (#264): the URL of the agent's memory namespace on the state
# API, dereferenced by the runner at boot, plus the API key it authenticates
# with. MEMORY_REF is the frozen ACI SessionConfig field; MEMORY_TOKEN is a
# runner-local knob (not part of the frozen env), like AGENTOS_RUNNER_TOKEN.
MEMORY_REF_ENV = "AGENTOS_MEMORY_REF"
MEMORY_TOKEN_ENV = "AGENTOS_MEMORY_TOKEN"
# The conversation-history port (#20, ADR-0029): the URL of THIS thread's
# transcript key on the same durable state store, dereferenced by the runner at
# boot to rehydrate the conversation after an unplanned restart, plus the API key
# it authenticates with. Both are runner-local knobs, NOT frozen ACI env.
HISTORY_REF_ENV = "AGENTOS_HISTORY_REF"
HISTORY_TOKEN_ENV = "AGENTOS_HISTORY_TOKEN"
BASE_URL_ENV = "ANTHROPIC_BASE_URL"
MODEL_ENV = "AGENTOS_MODEL"
# Per-claim bearer token the runner enforces on its ACI POST routes (issue #63).
# Not a model credential, so apply_model_env never sees it; minted fresh per claim.
RUNNER_TOKEN_ENV = "AGENTOS_RUNNER_TOKEN"
# Per-agent permission gates (#245, ADR-0010): comma-separated tool names whose
# calls the runner intercepts via can_use_tool and pauses awaiting approval.
# A runner-local knob (not frozen ACI env), like AGENTOS_IDEMPOTENT_TOOLS.
APPROVAL_REQUIRED_ENV = "AGENTOS_APPROVAL_REQUIRED_TOOLS"
# the worker re-mints every turn; this only bounds a leaked-token window (ADR-0033)
SANDBOX_TOKEN_TTL_SECONDS = 24 * 60 * 60

_RESOLVE_SQL = """
SELECT a.id AS agent_id,
       a.max_usd_per_day AS max_usd_per_day,
       a.max_output_tokens_per_run AS max_output_tokens_per_run,
       a.behavior_packs AS behavior_packs,
       a.model AS model,
       a.approval_required_tools AS approval_required_tools,
       a.approval_routes AS approval_routes,
       v.id AS version_id,
       v.version_label AS version_label,
       v.bundle_ref AS bundle_ref
FROM {schema}.agents a
JOIN {schema}.deployments d ON d.agent_id = a.id AND d.status = 'active'
JOIN {schema}.agent_versions v ON v.id = d.version_id AND v.agent_id = a.id
WHERE a.slack_channel = :channel
ORDER BY (d.environment = 'prod') DESC, d.deployed_at DESC
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
    # The agent's pinned model id (#254), forwarded as AGENTOS_MODEL at boot.
    # None falls back to the worker's configured default model.
    model: str | None = None
    # The agent's permission gates (#245): tool names requiring human approval,
    # forwarded as AGENTOS_APPROVAL_REQUIRED_TOOLS at boot. None means no gates.
    approval_required_tools: list[str] | None = None
    # The agent's approval route bindings (#247): manifest route name ->
    # workspace binding ({"channel": "C..."}), resolved by the kernel when a
    # raised approval names a route. None means no bindings.
    approval_routes: dict[str, Any] | None = None


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
            rows = result.mappings().all()
        if not rows:
            return None
        # The ORDER BY still picks one deterministic winner (prod-first, then most
        # recent), but if more than one *agent* is bound to this channel the others
        # silently never respond. Surface that instead of dropping them invisibly
        # (#38). One agent with both a dev and a prod deployment active is two rows
        # but one agent, so count distinct agents, not rows.
        distinct_agents = {r["agent_id"] for r in rows}
        if len(distinct_agents) > 1:
            chosen = rows[0]["agent_id"]
            shadowed = sorted(str(a) for a in distinct_agents if a != chosen)
            logger.warning(
                "channel %s has %d agents bound; routing to agent %s and shadowing "
                "%s (only one agent per channel responds; see issue #38)",
                channel,
                len(distinct_agents),
                chosen,
                ", ".join(shadowed),
            )
        data = dict(rows[0])
        # asyncpg returns JSONB as a str for a raw-text SELECT (no column type to
        # trigger SQLAlchemy's json deserializer); decode it to the dict/list the
        # model expects. A dict/list (or None) passes through untouched.
        packs = data.get("behavior_packs")
        if isinstance(packs, str):
            data["behavior_packs"] = json.loads(packs)
        gates = data.get("approval_required_tools")
        if isinstance(gates, str):
            data["approval_required_tools"] = json.loads(gates)
        routes = data.get("approval_routes")
        if isinstance(routes, str):
            data["approval_routes"] = json.loads(routes)
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
            RUNNER_TOKEN_ENV: secrets.token_urlsafe(32),
        }
        if resolved.bundle_ref is not None:
            env[BUNDLE_REF_ENV] = resolved.bundle_ref
        # Deliver the agent's permission gates (#245): the runner intercepts
        # these tool calls via can_use_tool and pauses awaiting approval.
        # Names are comma-joined (validated comma-free at the API on write).
        if resolved.approval_required_tools:
            env[APPROVAL_REQUIRED_ENV] = ",".join(resolved.approval_required_tools)
        # Deliver the memory ref (#264): the agent's scoped namespace on the
        # durable state store (#23/#248). The runner dereferences it at boot to
        # load prior memory and to append learned records with provenance. The
        # runner now receives a scoped ``state`` token (ADR-0033, #410) bound to
        # this agent, not the raw platform key, so a sandboxed agent cannot
        # resolve approvals or reach another agent's namespace.
        base = self._config.api_base_url.rstrip("/")
        env[MEMORY_REF_ENV] = f"{base}/agents/{resolved.agent_id}/state/memory"
        # Deliver the history ref (#20, ADR-0029): this thread's transcript key on
        # the same state store. It is deterministic per (agent, thread), so a
        # fresh, restarted, or resumed sandbox all boot with the same ref and the
        # runner rehydrates the conversation identically -- an unplanned restart
        # needs no special branch. thread_key is URL-encoded so a channel/ts with
        # reserved characters cannot break the key path.
        thread_segment = quote(thread_key, safe="")
        env[HISTORY_REF_ENV] = (
            f"{base}/agents/{resolved.agent_id}/state/transcript/{thread_segment}"
        )
        # Mint one scoped ``state`` token (ADR-0033, #410) for this agent and use
        # it for both the memory and history tokens. When no platform key is
        # configured (fake/local) there is nothing to sign with, so no token is
        # minted and neither is set -- preserving the pre-#410 no-key path.
        if self._config.api_key:
            state_token = sandbox_token.mint(
                self._config.api_key,
                agent=str(resolved.agent_id),
                scope="state",
                exp=int(time.time()) + SANDBOX_TOKEN_TTL_SECONDS,
            )
            env[MEMORY_TOKEN_ENV] = state_token
            env[HISTORY_TOKEN_ENV] = state_token
        # The agent's pinned model (#254) overrides the worker default; None
        # falls back to config.model inside apply_model_env.
        apply_model_env(env, self._config, model_override=resolved.model)
        return env


def apply_model_env(
    env: dict[str, str], config: WorkerConfig, model_override: str | None = None
) -> None:
    """Layer the runner model + credentials passthrough onto a boot env.

    Shared by the runs binding and the eval consumer so both lanes boot the
    runner the same way: fake_model gates the canned model (no credential
    needed); credentials is forwarded only when set and never logged. The local
    model demo path injects a generic Anthropic compatible base URL when
    configured; an explicit model is forwarded whenever set.

    ``model_override`` is the per-agent AGENTOS_MODEL (#254): when set it wins
    over the worker's configured default model, so a single agent can be pinned
    to a specific model. None means "use the platform default" (config.model).
    """
    if config.fake_model:
        env[FAKE_MODEL_ENV] = "1"
    if config.credentials:
        env[CREDENTIALS_ENV] = config.credentials
    if config.model_base_url:
        env[BASE_URL_ENV] = config.model_base_url
    model = model_override if model_override is not None else config.model
    if model:
        env[MODEL_ENV] = model
