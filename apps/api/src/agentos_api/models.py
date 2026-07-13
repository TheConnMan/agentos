"""SQLAlchemy models: agents, agent_versions, deployments.

Kept deliberately minimal (see docs/build-orchestration-plan.md). B2 added the
bundle columns; J1 added the git-flow columns (agents.repo_full_name,
agent_versions.commit_sha, deployments.bot_identity/commit_sha).
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Enum, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import SCHEMA, Base


class Environment(enum.StrEnum):
    prod = "prod"
    dev = "dev"


class ApprovalStatus(enum.StrEnum):
    """Lifecycle of an approval. ``pending`` is the only non-terminal value; a
    resolve compare-and-sets it to ``approved`` or ``rejected`` exactly once."""

    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(unique=True)
    slack_channel: Mapped[str]
    # The GitHub repo (owner/name) whose pushes deploy this agent (J1).
    repo_full_name: Mapped[str | None] = mapped_column(
        default=None, unique=True, index=True
    )
    # Per-agent budget (L1). Field names match the frozen ACI SessionConfig
    # AGENTOS_BUDGET so the worker passes them straight through at sandbox boot;
    # NULL means platform defaults apply.
    max_usd_per_day: Mapped[float | None] = mapped_column(default=None)
    max_output_tokens_per_run: Mapped[int | None] = mapped_column(default=None)
    # Per-agent model id (#254). Forwarded as AGENTOS_MODEL at sandbox boot so a
    # single agent can be pinned to a specific model (BYO-model, #24); NULL means
    # the platform/worker default model applies. The value is passed straight
    # through to the runner, which resolves it against its configured provider.
    model: Mapped[str | None] = mapped_column(default=None)
    # Per-agent behavior packs: declarative, opt-in UX touches the worker applies
    # around a turn (a sampled "working..." line, a canned greeting reply). Stored
    # as JSON here and resolved onto the deployment by the worker's binding layer;
    # NULL means no packs (the platform default). The shape is validated by
    # schemas.BehaviorPacksConfig on write and parsed by
    # agentos_worker.behaviorpacks.BehaviorPacks on read.
    behavior_packs: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, default=None
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    versions: Mapped[list["AgentVersion"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )


class AgentVersion(Base):
    __tablename__ = "agent_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.agents.id", ondelete="CASCADE"), index=True
    )
    version_label: Mapped[str]
    bundle_ref: Mapped[str | None] = mapped_column(default=None)
    bundle_sha256: Mapped[str | None] = mapped_column(default=None)
    # The git commit this version was built from (J1); lets promote reuse the
    # already-built bundle instead of rebuilding.
    commit_sha: Mapped[str | None] = mapped_column(default=None, index=True)
    created_by: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    agent: Mapped[Agent] = relationship(back_populates="versions")


class Deployment(Base):
    __tablename__ = "deployments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.agents.id", ondelete="CASCADE"), index=True
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.agent_versions.id", ondelete="CASCADE"), index=True
    )
    environment: Mapped[Environment] = mapped_column(
        Enum(Environment, name="environment", schema=SCHEMA)
    )
    # The Slack bot identity this deployment routes to (J1): @agentos-dev for
    # dev, @agentos for prod.
    bot_identity: Mapped[str | None] = mapped_column(default=None)
    commit_sha: Mapped[str | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(server_default="active")
    deployed_at: Mapped[datetime] = mapped_column(server_default=func.now())


class WorkflowStateEntry(Base):
    """Durable, agent-scoped key/value state (#23, first slice).

    Cross-turn business state (a pending-approvals map, a dedupe seen-set) has
    nowhere durable to live today: sandboxes do not survive suspend, so agents
    keep it in-process and lose it on restart. This is a small scoped store --
    namespace + key per agent, an arbitrary-JSON value, and a monotonic
    ``version`` for compare-and-set. Backed by Postgres JSONB (no new datastore).
    Exposing it to bundle code via an auto-mounted MCP server is a later slice;
    this lands the store and its HTTP API.
    """

    __tablename__ = "workflow_state_entries"
    __table_args__ = (
        UniqueConstraint("agent_id", "namespace", "key", name="uq_state_agent_ns_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.agents.id", ondelete="CASCADE")
    )
    namespace: Mapped[str]
    key: Mapped[str]
    # Any JSON value: an object (a pending-approvals map), an array (a log
    # grown by append, #248), or a scalar. JSONB stores all of them.
    value: Mapped[Any] = mapped_column(JSONB)
    # Monotonic per-entry counter for compare-and-set: a put may pass the version
    # it last read, and the write is rejected if the stored version moved on.
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )


class Approval(Base):
    """A durable human-in-the-loop approval request (#22, ADR-0010).

    Created ``pending`` by the worker when a session pauses on an approval gate
    (ACI ``SessionStatus.awaiting-approval``) and resolved once, server-side, by
    the resolve endpoint. The record is the source of truth that outlives the
    suspend/resume of the paused session and every component restart: it holds
    both what is needed to resume (``conversation_id``, ``session_id``, and the
    reply handle) and the gate details shown to the approver (``tool``,
    ``prompt``). Resolve is a compare-and-set on ``status`` (``pending`` ->
    ``approved``/``rejected``), so the loser of a click race is told it was
    already resolved, and a resolver may never be the requester (self-approval is
    blocked). ``status`` is a plain string (not a DB enum) to keep the migration
    minimal; ``ApprovalStatus`` carries the values in code.
    """

    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint(
            "agent_id",
            "conversation_id",
            "tool_use_id",
            name="uq_approval_agent_conv_tooluse",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.agents.id", ondelete="CASCADE"), index=True
    )
    # Routing to resume the paused session: the thread key the run belongs to and
    # the SDK session id the replacement runner rehydrates from (ACI Final.session_id).
    conversation_id: Mapped[str]
    session_id: Mapped[str | None] = mapped_column(default=None)
    # The reply handle the resumed turn's output is delivered through, stored so a
    # resume days later (past the Valkey route TTL) can still reconstruct it.
    channel: Mapped[str]
    reply_placeholder: Mapped[str]
    reply_endpoint: Mapped[str | None] = mapped_column(default=None)
    # The gated tool call, surfaced to the approver on the card.
    tool: Mapped[str]
    tool_use_id: Mapped[str]
    input_digest: Mapped[str]
    prompt: Mapped[str]
    status: Mapped[str] = mapped_column(
        default=ApprovalStatus.pending.value,
        server_default=ApprovalStatus.pending.value,
        index=True,
    )
    # Who triggered the request (the turn's author); a resolver equal to this is
    # blocked as self-approval.
    requested_by: Mapped[str]
    resolved_by: Mapped[str | None] = mapped_column(default=None)
    resolved_at: Mapped[datetime | None] = mapped_column(default=None)
    # Set when the resumed turn has been enqueued, so the reconcile sweep never
    # resumes the same approval twice.
    resumed_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
