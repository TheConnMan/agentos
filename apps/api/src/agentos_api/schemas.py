"""Pydantic v2 request/response models for the API surface."""

import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import Environment

# Slack channel IDs start with C (public/private channel), D (DM), or G (legacy
# private group) followed by uppercase-alphanumeric chars. Allowlist-shaped on
# purpose: unlike the CLI's blocklist (which only rejects a leading '#'), this
# also rejects bare names ("general"), pasted URLs, and lowercase IDs -- none of
# which the worker can route on.
_SLACK_CHANNEL_ID = re.compile(r"^[CDG][A-Z0-9]{7,}$")


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


class AgentCreate(BaseModel):
    name: str
    slack_channel: str
    repo_full_name: str | None = None
    behavior_packs: BehaviorPacksConfig | None = None
    # Per-agent model id, forwarded as AGENTOS_MODEL at boot (#254). None uses the
    # platform default model.
    model: str | None = None

    _check_slack_channel = field_validator("slack_channel")(
        _validate_slack_channel_id
    )


class AgentUpdate(BaseModel):
    """Partial update of mutable agent fields. slack_channel and model are
    updatable (name and repo binding are identity); an omitted field is left
    unchanged."""

    slack_channel: str | None = None
    # New per-agent model id (#254). Omitted (None) leaves the current model
    # unchanged, matching the slack_channel convention.
    model: str | None = None

    _check_slack_channel = field_validator("slack_channel")(
        _validate_slack_channel_id
    )


class AgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slack_channel: str
    repo_full_name: str | None
    behavior_packs: dict[str, Any] | None
    model: str | None
    created_at: datetime


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


class TrajectorySpecRequest(BaseModel):
    """One strict trajectory scorer specification supplied by an API caller."""

    model_config = ConfigDict(extra="forbid")

    expected: list[str]
    mode: Literal["exact", "in_order", "any_order", "precision", "recall"] = (
        "in_order"
    )
    threshold: float = Field(default=1.0, ge=0.0, le=1.0, allow_inf_nan=False)

    @field_validator("expected", mode="before")
    @classmethod
    def _expected_is_string_list(cls, value: object) -> object:
        if not isinstance(value, list) or not all(
            isinstance(tool, str) for tool in value
        ):
            raise ValueError("expected must be a string list")
        return value

    @field_validator("threshold", mode="before")
    @classmethod
    def _threshold_is_number(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("threshold must be a number")
        return value


class TrajectorySelectionRequest(BaseModel):
    """Optional explicit trajectory selection and its eval case identity."""

    trajectory_specs: dict[str, TrajectorySpecRequest] | None = None
    case_ids: (
        Annotated[
            list[Annotated[str, Field(min_length=1)]],
            Field(min_length=1, json_schema_extra={"uniqueItems": True}),
        ]
        | None
    ) = None
    cases_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("case_ids", mode="before")
    @classmethod
    def _case_ids_is_string_list(cls, value: object) -> object:
        if value is None:
            return value
        if not isinstance(value, list) or not all(
            isinstance(case_id, str) for case_id in value
        ):
            raise ValueError("case_ids must be a string list")
        return value

    @model_validator(mode="after")
    def _identity_matches_selection(self) -> Self:
        if self.trajectory_specs is None:
            if self.case_ids is not None or self.cases_sha256 is not None:
                raise ValueError("case identity requires trajectory_specs")
            return self

        if self.case_ids is None or self.cases_sha256 is None:
            raise ValueError("trajectory_specs require case identity")
        if len(set(self.case_ids)) != len(self.case_ids):
            raise ValueError("case_ids must be unique")
        return self


class EvalTriggerRequest(TrajectorySelectionRequest):
    """Ask for an on-demand platform eval run for an agent (issue #10).

    Enqueues the same EvalJobRequest the git-push fan-out uses, minus the
    push-only gate. With no version_id the agent's active dev deployment is
    evaluated; suite falls back to Settings.eval_default_suite when omitted.
    """

    agent_id: uuid.UUID
    version_id: uuid.UUID | None = None
    suite: str | None = None
    target_url: str | None = None


class EvalTriggerResult(BaseModel):
    """The enqueued eval job's stream id plus the resolved job identity."""

    stream_id: str
    agent_id: uuid.UUID
    version_id: uuid.UUID
    sha: str
    suite: str
    bundle_ref: str | None


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

    ``index`` is the entry's position in the append-only log; it is the stable
    handle the edit/delete endpoints (#267) address, and it survives as long as
    the log is not consolidated (#265) or reordered.
    """

    index: int
    content: str
    provenance: MemoryProvenanceOut


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
    """Edit the ``content`` of one memory entry (#267); provenance is preserved."""

    content: str
