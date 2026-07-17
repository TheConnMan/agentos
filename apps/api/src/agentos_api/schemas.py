"""Pydantic v2 request/response models for the API surface."""

import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from plugin_format import is_reserved_boot_env_name
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from .models import Environment

# Slack channel IDs start with C (public/private channel), D (DM), or G (legacy
# private group) followed by uppercase-alphanumeric chars. Allowlist-shaped on
# purpose: unlike the CLI's blocklist (which only rejects a leading '#'), this
# also rejects bare names ("general"), pasted URLs, and lowercase IDs -- none of
# which the worker can route on.
_SLACK_CHANNEL_ID = re.compile(r"^[CDG][A-Z0-9]{7,}$")
# Slack user-group (subteam) IDs start with S; user IDs start with U, or W for
# enterprise-grid users. Same allowlist discipline and same reason as channels:
# a @handle or a bare name never resolves, and the S/C prefix is the whole
# distinction between a user group and a channel.
_SLACK_USERGROUP_ID = re.compile(r"^S[A-Z0-9]{7,}$")
_SLACK_USER_ID = re.compile(r"^[UW][A-Z0-9]{7,}$")


def _validate_slack_channel_id(value: str | None) -> str | None:
    """Enforce a Slack channel-ID shape on a binding. Slack events carry the
    channel ID (e.g. C0123ABCD) and the worker routes on it, so a #name or
    bare-name binding never receives messages. This is the authoritative gate
    for every caller (UI, API, CLI); the CLI keeps a fast local check purely
    for UX. Reused by AgentCreate and AgentUpdate so create and PATCH validate
    identically; None (an omitted PATCH field) passes through as a no-op."""
    if value is None:
        return value
    if not _SLACK_CHANNEL_ID.match(value):
        raise ValueError(
            f"slack channel {value!r} is not a Slack channel ID: real Slack "
            "events carry the channel ID (e.g. C0123ABCD) and the worker "
            "routes on it, so a #name or bare-name binding never receives "
            "messages. Pass the channel ID instead -- find it in the channel's "
            "About tab, or the channel URL (.../archives/C0123ABCD)."
        )
    return value


class AppConfig(BaseModel):
    """Open app-level config the UI reads before auth (org/workspace name)."""

    org_name: str


class LoadPackConfig(BaseModel):
    """Rotating "working..." load lines for one agent. Mirrors the worker's
    agentos_worker.behaviorpacks.LoadPack (packs ride on agent config, not the
    frozen ACI contract, so the shape is duplicated across the layers the way
    BudgetConfig mirrors the ACI Budget)."""

    enabled: bool = False
    lines: list[str] = []


class TipsPackConfig(BaseModel):
    """Rotating capability tips for one agent (mirrors behaviorpacks.TipsPack).
    Separate from LoadPackConfig: a load line is what the agent is doing now, a
    tip advertises what it can do."""

    enabled: bool = False
    tips: list[str] = []


class GreetingPackConfig(BaseModel):
    """The deterministic greeting short-circuit content for one agent."""

    enabled: bool = False
    phrases: list[str] = []
    reply: str = ""


class HelpPackConfig(BaseModel):
    """The deterministic help / "what can you do" short-circuit for one agent."""

    enabled: bool = False
    phrases: list[str] = []
    reply: str = ""


class SettingConfig(BaseModel):
    """One declared user-editable runtime knob (mirrors behaviorpacks.Setting)."""

    key: str
    label: str = ""
    kind: str = "str"
    default: str = ""
    help: str = ""
    choices: list[str] = []
    applies_live: bool = True


class SettingsPackConfig(BaseModel):
    """An agent's declarative allowlist of editable runtime knobs (schema only;
    the override store + edit UI are a deferred runtime)."""

    enabled: bool = False
    settings: list[SettingConfig] = []


class NavPackConfig(BaseModel):
    """The no-dead-ends hub button for one agent (mirrors behaviorpacks.NavPack)."""

    enabled: bool = False
    hub_label: str = ""
    hub_command: str = ""


class BehaviorPacksConfig(BaseModel):
    """An agent's opt-in behavior packs. Validated on write and stored as JSON on
    the agent row; the worker parses the same JSON at bind time."""

    model_config = ConfigDict(from_attributes=True)

    load: LoadPackConfig = LoadPackConfig()
    tips: TipsPackConfig = TipsPackConfig()
    greeting: GreetingPackConfig = GreetingPackConfig()
    help: HelpPackConfig = HelpPackConfig()
    settings: SettingsPackConfig = SettingsPackConfig()
    nav: NavPackConfig = NavPackConfig()


def _validate_tool_names(value: list[str] | None) -> list[str] | None:
    """Approval-required tool names (#245) must be non-empty, comma-free
    strings: the worker forwards the list to the runner as a comma-separated
    AGENTOS_APPROVAL_REQUIRED_TOOLS value, so a comma inside a name would
    silently split into two wrong gates."""
    if value is None:
        return value
    cleaned = [t.strip() for t in value]
    if any(not t or "," in t for t in cleaned):
        raise ValueError(
            "approval_required_tools entries must be non-empty tool names "
            "without commas (e.g. Bash, mcp__github__create_issue)"
        )
    return cleaned


_SECRET_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _validate_secret_map(value: dict[str, str] | None) -> dict[str, str] | None:
    """Per-agent connector secrets (ADR-0009, #429): keys are env-var-style
    NAMES, values the secret material the worker forwards into the sandbox env.
    A non-env-var name cannot be forwarded (and would break ``.mcp.json``
    ``${VAR}`` expansion); an empty value is a misconfigure that fails connector
    auth silently, so both are rejected on write."""
    if value is None:
        return value
    for name, secret in value.items():
        if not _SECRET_NAME_RE.match(name):
            raise ValueError(
                f"secret name {name!r} must be an env-var-style name "
                "(uppercase letters, digits, underscore; not starting with a digit)"
            )
        if is_reserved_boot_env_name(name):
            # Reserved names are either AGENTOS_*-prefixed platform sandbox
            # boot-env keys (budget/session/credential/etc.) or one of the
            # fixed model-credential keys (ANTHROPIC_API_KEY, etc.). A
            # connector secret named that way would either clobber a boot var
            # or be silently dropped by the worker binding's reserved-key
            # guard, so reject it on write.
            raise ValueError(
                f"secret name {name!r} is reserved: it is a platform boot-env, "
                "model-credential, or redirect/capture-capable key and cannot be "
                "used for a connector secret"
            )
        if not secret:
            raise ValueError(f"secret {name!r} has an empty value")
    return value


class _StoredWithoutNulls(BaseModel):
    """Serializes to the stored-JSONB shape: unset keys are absent, not null.

    Route bindings are dumped straight into ``agents.approval_routes`` by every
    persist site, and a plain dump would rewrite every pre-#420 binding with an
    ``approvers: null`` sibling (and every group-only approvers block with a
    ``users: null`` one) on the next write. Making that an invariant of the
    models themselves, rather than asking each caller for ``exclude_none=True``,
    keeps the stored shape from depending on every writer remembering.

    Tripwire for a future reader: subclasses are validation-side only today
    (request bodies), which is why the committed ``openapi.json`` carries one
    schema each. Using one in a RESPONSE model would make FastAPI split it into
    ``-Input``/``-Output`` variants, because the wrap serializer above makes the
    dumped shape differ from the validated one.
    """

    @model_serializer(mode="wrap")
    def _dump_without_nulls(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        return {k: v for k, v in handler(self).items() if v is not None}


class ApprovalApprovers(_StoredWithoutNulls):
    """WHO may resolve a route's approvals (#420), as opposed to the binding's
    ``channel``, which is only WHERE the card posts.

    Declaring an approvers block is what lets a request sit in a broad channel
    where everyone can see it while only a narrow set may act on it. Omitting it
    keeps the zero-setup default: the card channel's members are the approvers.
    """

    # A typo in an optional key must not be ignored: silently dropping it would
    # leave no approvers block at read time, falling the route back to channel
    # membership and widening the approver set the operator meant to narrow.
    model_config = ConfigDict(extra="forbid")

    # A Slack user group whose current members are the approvers. Membership is
    # resolved by the API against Slack at resolve time, never asserted by the
    # caller. Ignored when ``users`` is set.
    group: str | None = None
    # An explicit allowlist of Slack user IDs. Takes precedence over ``group``
    # (issue #420 settles the precedence rather than refusing the combination),
    # and needs no Slack lookup at all.
    users: list[str] | None = None

    @field_validator("group")
    @classmethod
    def _check_group(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not _SLACK_USERGROUP_ID.match(value):
            raise ValueError(
                f"approvers group {value!r} is not a Slack user-group ID: pass "
                "the ID (e.g. S0123ABCD), not a @handle or a name -- a handle "
                "never resolves, and a C-prefixed value is a channel, not a "
                "user group. Find it via the usergroups.list API."
            )
        return value

    @field_validator("users")
    @classmethod
    def _check_users(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        if not value:
            # Neither "unset" (omit the key) nor "nobody may approve": as silent
            # config the latter is a footgun, since the approval could then only
            # ever expire.
            raise ValueError(
                "approvers users, when present, must contain at least one user ID"
            )
        for user in value:
            if not _SLACK_USER_ID.match(user):
                raise ValueError(
                    f"approvers user {user!r} is not a Slack user ID: pass the "
                    "ID (e.g. U0123ABCD, or W0123ABCD on enterprise grid), not "
                    "a @handle or a display name."
                )
        return value

    @model_validator(mode="after")
    def _check_not_empty(self) -> "ApprovalApprovers":
        if self.group is None and self.users is None:
            raise ValueError(
                "approvers must declare at least one of group or users; omit "
                "the approvers block entirely to keep channel membership"
            )
        return self


class ApprovalRouteBinding(_StoredWithoutNulls):
    """One workspace binding for a manifest-declared approval route (#247):
    the Slack channel whose members are that route's approvers (under the
    channel-membership authorizer), and optionally the ``approvers`` block that
    narrows WHO may act (#420), leaving ``channel`` to mean only WHERE the card
    posts.
    """

    # Rejects a typo'd ``approver`` rather than storing a channel-only binding
    # the operator believes narrows authority. Pre-#420 bindings are
    # ``{"channel": ...}`` only, so forbidding extras does not reject them.
    model_config = ConfigDict(extra="forbid")

    channel: str
    approvers: ApprovalApprovers | None = None

    _check_channel = field_validator("channel")(_validate_slack_channel_id)


def _validate_route_names(
    value: dict[str, ApprovalRouteBinding] | None,
) -> dict[str, ApprovalRouteBinding] | None:
    """Route names must be non-empty; they are matched verbatim against the
    manifest's declared route names."""
    if value is None:
        return value
    if any(not name.strip() for name in value):
        raise ValueError("approval_routes keys must be non-empty route names")
    return value


class AgentCreate(BaseModel):
    name: str
    slack_channel: str
    repo_full_name: str | None = None
    behavior_packs: BehaviorPacksConfig | None = None
    # Per-agent model id, forwarded as AGENTOS_MODEL at boot (#254). None uses the
    # platform default model.
    model: str | None = None
    # Per-agent permission gates (#245): tool names requiring human approval.
    # None means no gates (the bypass posture).
    approval_required_tools: list[str] | None = None
    # Per-agent approval route bindings (#247): manifest route name -> workspace
    # channel. None means no bindings (unbound routes fall back to the
    # requesting channel).
    approval_routes: dict[str, ApprovalRouteBinding] | None = None
    # Per-agent connector secret VALUES (ADR-0009, #429): env-var-style name ->
    # secret. Stored on the agent row for the local tier and forwarded into the
    # sandbox by the worker binding. None means no connector secrets.
    secrets: dict[str, str] | None = None

    _check_slack_channel = field_validator("slack_channel")(
        _validate_slack_channel_id
    )
    _check_approval_tools = field_validator("approval_required_tools")(
        _validate_tool_names
    )
    _check_approval_routes = field_validator("approval_routes")(
        _validate_route_names
    )
    _check_secrets = field_validator("secrets")(_validate_secret_map)


class AgentUpdate(BaseModel):
    """Partial update of mutable agent fields. slack_channel, model, and
    approval_required_tools are updatable (name and repo binding are identity);
    an omitted field is left unchanged."""

    slack_channel: str | None = None
    # New per-agent model id (#254). Omitted (None) leaves the current model
    # unchanged, matching the slack_channel convention.
    model: str | None = None
    # New permission gates (#245). Omitted (None) leaves the current gates
    # unchanged; an explicit empty list clears them.
    approval_required_tools: list[str] | None = None
    # New route bindings (#247). Omitted (None) leaves the current bindings
    # unchanged; an explicit empty dict clears them.
    approval_routes: dict[str, ApprovalRouteBinding] | None = None
    # New connector secrets (#429). Omitted (None) leaves current secrets
    # unchanged; an explicit empty dict clears them.
    secrets: dict[str, str] | None = None

    _check_slack_channel = field_validator("slack_channel")(
        _validate_slack_channel_id
    )
    _check_approval_tools = field_validator("approval_required_tools")(
        _validate_tool_names
    )
    _check_approval_routes = field_validator("approval_routes")(
        _validate_route_names
    )
    _check_secrets = field_validator("secrets")(_validate_secret_map)


class AgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slack_channel: str
    repo_full_name: str | None
    behavior_packs: dict[str, Any] | None
    model: str | None
    approval_required_tools: list[str] | None
    approval_routes: dict[str, Any] | None
    # Connector secret NAMES only (#429) -- values are never returned. The stored
    # column is a name->value map; expose just the sorted names so an operator can
    # see which secrets an agent has bound without the material leaving the API.
    secrets: list[str] | None
    created_at: datetime

    @field_validator("secrets", mode="before")
    @classmethod
    def _secret_names_only(cls, value: Any) -> Any:
        return sorted(value) if isinstance(value, dict) else value


class GraderOut(BaseModel):
    """A deterministic grader, mirroring the frozen eval-case Grader shape
    (`apps/worker/schema/eval-cases.schema.json`). Do not let this drift from the
    worker's `Grader` model."""

    kind: Literal["exact", "contains", "regex"]
    expected: str
    case_sensitive: bool = False


class EvalCaseOut(BaseModel):
    """An eval case conforming to the frozen eval-case format (#8, ADR-0019):
    an input prompt plus the grader that judges the answer. Emitted by the
    promote-a-trace-to-an-eval-case endpoint (#259)."""

    id: str
    input: str
    grader: GraderOut


class VersionCreate(BaseModel):
    version_label: str
    bundle_ref: str | None = None
    created_by: str


class VersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    version_label: str
    bundle_ref: str | None
    bundle_sha256: str | None
    commit_sha: str | None
    created_by: str
    created_at: datetime


class BundleOut(BaseModel):
    """Result of storing a bundle for a version."""

    version_id: uuid.UUID
    bundle_ref: str
    bundle_sha256: str
    size_bytes: int


class BundleValidationError(BaseModel):
    """Returned (HTTP 422) when a bundle fails plugin-format validation."""

    detail: str = "bundle failed validation"
    errors: list[dict[str, str]]


class BundleFile(BaseModel):
    """One text file inside a stored bundle (path relative to the bundle root)."""

    path: str
    content: str


class BundleFiles(BaseModel):
    """The readable text surfaces of a version's stored bundle."""

    files: list[BundleFile]


class DeploymentCreate(BaseModel):
    agent_id: uuid.UUID
    version_id: uuid.UUID
    environment: Environment
    status: str = "active"


class DeploymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    version_id: uuid.UUID
    environment: Environment
    bot_identity: str | None
    commit_sha: str | None
    status: str
    deployed_at: datetime


class ApprovalCreate(BaseModel):
    """A durable approval request (#244), created by the worker when a run ends
    awaiting-approval. ``dedupe_key`` is the triggering event id, so a
    redelivered turn adopts the existing record instead of forking a second one.
    ``route``/``card_channel`` (#247) record the manifest route the request
    named and the channel the worker routed the card to after binding
    resolution; the authorizer proves membership against ``card_channel``."""

    agent_id: uuid.UUID | None = None
    conversation_id: str = Field(min_length=1)
    author: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    reply_channel: str = Field(min_length=1)
    reply_placeholder: str = Field(min_length=1)
    reply_endpoint: str | None = None
    dedupe_key: str = Field(min_length=1)
    route: str | None = None
    card_channel: str | None = None
    # Durable gate provenance (#544, Decision C), written by the runner. Both
    # optional: an older runner emits neither and the row's columns stay NULL
    # (the rolling-deploy window the worker's prefix fallback covers).
    gate_kind: Literal["permission", "policy"] | None = None
    granted_tool: str | None = None
    # Optional SLA: seconds from creation after which the record can only
    # expire, never be approved or rejected.
    expires_in_seconds: int | None = Field(default=None, gt=0)


class ApprovalResolve(BaseModel):
    """One resolution attempt. Exactly one attempt wins (compare-and-set), and
    the server-side authorizer (#246) decides first whether this actor may
    resolve at all: self-approval is blocked, and channel membership is proven
    by ``actor_channel`` -- the channel the resolution attempt was made from
    (the card click's channel, relayed by the dispatcher; asserted explicitly
    by API-key operators)."""

    decision: Literal["approved", "rejected"]
    resolved_by: str = Field(min_length=1)
    note: str | None = None
    actor_channel: str | None = None


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID | None
    conversation_id: str
    author: str
    summary: str
    reply_channel: str
    reply_placeholder: str
    reply_endpoint: str | None
    dedupe_key: str
    route: str | None
    card_channel: str | None
    # Gate provenance (#544): which gate fired, and the tool a permission-gate
    # grant is bound to. Both NULL for a policy gate or a pre-#544 row.
    gate_kind: str | None
    granted_tool: str | None
    status: str
    expires_at: datetime | None
    resolved_by: str | None
    resolution_note: str | None
    created_at: datetime
    resolved_at: datetime | None


class ApprovalAuditOut(BaseModel):
    """One audit entry (#247): who attempted what, and the authorizer snapshot
    that counted (or refused) them."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    approval_id: uuid.UUID
    action: str
    actor: str
    actor_channel: str | None
    decision: str
    authorizer: str
    authorized: bool
    reason: str | None
    # The membership facts that decided it (#420): the group and the actor's
    # verdict, the allowlist that counted, or the channels compared. NULL for
    # writers that made no membership decision (the expiry sweeper) and for rows
    # written before the column existed.
    evidence: dict[str, Any] | None
    created_at: datetime


class WebhookResult(BaseModel):
    """The outcome of processing a GitHub webhook event."""

    status: str
    environment: Environment | None = None
    bot_identity: str | None = None
    agent_id: uuid.UUID | None = None
    version_id: uuid.UUID | None = None
    deployment_id: uuid.UUID | None = None
    commit_sha: str | None = None
    errors: list[dict[str, str]] | None = None


class ObservationNode(BaseModel):
    """One node in the reconstructed observation tree (Runs view)."""

    id: str
    type: str
    name: str | None = None
    startTime: str | None = None  # noqa: N815 (Langfuse wire field name)
    model: str | None = None
    usageDetails: dict[str, Any] | None = None  # noqa: N815
    children: list["ObservationNode"] = []


class TraceTree(BaseModel):
    """A trace plus its reconstructed observation tree."""

    trace: dict[str, Any]
    tree: list[ObservationNode]
    # The runner's sandbox id (agentos.sandbox_id), hoisted out of the trace/
    # observation resource attributes; None when the trace predates the attr.
    sandbox_id: str | None = None


class MetricsSummary(BaseModel):
    """Scalar totals for the Metrics tab stat row over a time window."""

    start: str
    end: str
    runs: int
    latency_p95_ms: float
    tokens: int
    cost_usd: float
    error_rate: float


class MetricPoint(BaseModel):
    ts: str
    value: float


class MetricSeries(BaseModel):
    """One metric as a time series for the Metrics tab charts."""

    metric: str
    granularity: str
    start: str
    end: str
    points: list[MetricPoint]


class PodLogs(BaseModel):
    """Runner-pod logs for the per-run runner-logs affordance."""

    namespace: str
    pod: str
    container: str | None
    logs: str


class RunnerPods(BaseModel):
    """The runner sandbox pods in a namespace (populates the Logs pod dropdown)."""

    namespace: str
    pods: list[str]


class EvalCell(BaseModel):
    """One cell of the eval matrix: a case's result on a version.

    ``model`` is the model the result was produced under (the matrix's model
    dimension), or ``None`` when the recording run carried no model tag.
    """

    version: str
    status: Literal["pass", "fail", "missing"]
    model: str | None = None


class EvalMatrixRow(BaseModel):
    """One row of the eval matrix: a case across every version column."""

    case_id: str
    cells: list[EvalCell]


class EvalModelSummary(BaseModel):
    """A per-model rollup across the suite: pass-rate and total cost.

    The model dimension of the matrix (issue #255): the same suite run across
    models is sliceable here into ``passed/total`` pass-rate and summed
    ``cost_usd`` per model, so BYO-model work can compare which models a use case
    tolerates and at what cost. ``cost_usd`` is ``None`` when no case under this
    model reported a cost (e.g. the fake-model path), rather than a misleading 0.
    """

    model: str | None = None
    passed: int
    total: int
    cost_usd: float | None = None

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


class EvalMatrix(BaseModel):
    """The eval matrix grid: rows = cases, columns = versions (most recent first).

    ``models`` and ``model_summaries`` add the model dimension: the distinct
    models observed across the fetched traces, and a pass-rate + cost rollup per
    model for BYO-model comparison. They are additive; the version grid is
    unchanged.
    """

    suite: str
    versions: list[str]
    cases: list[str]
    rows: list[EvalMatrixRow]
    models: list[str | None] = []
    model_summaries: list[EvalModelSummary] = []


class EvalTriggerRequest(BaseModel):
    """Ask for an on-demand platform eval run for an agent (issue #10).

    Enqueues the same EvalJobRequest the git-push fan-out uses, minus the
    push-only gate. With no version_id the agent's active dev deployment is
    evaluated; suite falls back to Settings.eval_default_suite when omitted.
    """

    agent_id: uuid.UUID
    version_id: uuid.UUID | None = None
    suite: str | None = None
    target_url: str | None = None
    # The model to evaluate under (#526): booted into the eval sandbox and used as
    # the run's matrix model dimension. None uses the worker default. A sweep posts
    # one trigger per model, then reads GET /evals/matrix sliced by model back.
    model: str | None = None


class EvalTriggerResult(BaseModel):
    """The enqueued eval job's stream id plus the resolved job identity."""

    stream_id: str
    agent_id: uuid.UUID
    version_id: uuid.UUID
    sha: str
    suite: str
    bundle_ref: str | None
    # Echoes the requested model (#526) so a sweep caller can key each enqueued
    # job to the model it will land under in the matrix; None = worker default.
    model: str | None = None


class EvalReport(BaseModel):
    """An eval run's rollup, reported so the API can post the PR check."""

    repo_full_name: str
    sha: str
    passed_count: int
    total: int
    target_url: str | None = None


class EvalReportResult(BaseModel):
    """The committed GitHub commit-status state for a reported eval run."""

    state: str
    sha: str


class BudgetConfig(BaseModel):
    """Per-agent budget (L1). Field names match the ACI AGENTOS_BUDGET so the
    worker passes them straight through; null means platform defaults."""

    model_config = ConfigDict(from_attributes=True)

    max_usd_per_day: Annotated[float, Field(gt=0)] | None = None
    max_output_tokens_per_run: Annotated[int, Field(gt=0)] | None = None


class KillState(BaseModel):
    """Whether an agent is currently killed (kill switch, L1)."""

    killed: bool


class CostReport(BaseModel):
    """Daily spend series + total for an agent (L1 Cost view)."""

    start: str
    end: str
    total_usd: float
    points: list[MetricPoint]


class StateEntryPut(BaseModel):
    """Write a durable state entry (#23). ``expected_version`` opts into
    compare-and-set: the write is rejected with 409 unless it matches the stored
    version (omit it for a blind upsert). ``value`` is any JSON value (object,
    array, or scalar); an array value is what ``append`` grows."""

    value: Any
    expected_version: int | None = None


class StateAppendIn(BaseModel):
    """Append ``item`` to a log-shaped (JSON array) state entry (#248). If the
    entry does not exist it is created as a single-element array; if it exists
    its value must already be an array, else the append is rejected."""

    item: Any


class StateEntryOut(BaseModel):
    """A durable state entry as returned to the caller."""

    model_config = ConfigDict(from_attributes=True)

    namespace: str
    key: str
    value: Any
    version: int
    updated_at: datetime


# --- Agent memory (#266 trace-back; #267 inspect/edit/delete) ---------------


class MemoryProvenanceOut(BaseModel):
    """Where a memory entry was learned from (#264 ``Provenance`` shape)."""

    learned_from_session_id: str | None = None
    source_trace_ids: list[str] = Field(default_factory=list)
    recorded_at: str = ""


class SourceTraceOut(BaseModel):
    """One resolved source trace: its id plus a link to view it in Langfuse."""

    trace_id: str
    trace_url: str


class MemoryEntryOut(BaseModel):
    """One learned memory entry as returned to an operator.

    ``index`` is the entry's current position in the memory log. It is valid for
    mutation only with this response's parent log ``version``. A mutation with
    a stale version conflicts if another change has reordered the log.
    """

    index: int
    content: str
    provenance: MemoryProvenanceOut
    version: int


class MemoryTraceBackOut(BaseModel):
    """The learned-from trace-back for one memory entry (#266).

    Resolves an entry's recorded provenance into the concrete session and source
    traces the lesson was learned from -- the answer to "how did it learn that?".
    """

    index: int
    content: str
    learned_from_session_id: str | None = None
    recorded_at: str = ""
    source_traces: list[SourceTraceOut] = Field(default_factory=list)


class MemoryEntryEdit(BaseModel):
    """Edit one memory entry using its parent log version.

    The required version prevents a stale positional index from changing an
    entry after the log has changed. Provenance is preserved.
    """

    content: str
    expected_version: int
