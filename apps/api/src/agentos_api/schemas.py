"""Pydantic v2 request/response models for the API surface."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import Environment


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


class AgentUpdate(BaseModel):
    """Partial update of a mutable agent field. Only slack_channel is updatable
    (name and repo binding are identity); an omitted field is left unchanged."""

    slack_channel: str | None = None


class AgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slack_channel: str
    repo_full_name: str | None
    behavior_packs: dict[str, Any] | None
    created_at: datetime


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
    """One cell of the eval matrix: a case's result on a version."""

    version: str
    status: Literal["pass", "fail", "missing"]


class EvalMatrixRow(BaseModel):
    """One row of the eval matrix: a case across every version column."""

    case_id: str
    cells: list[EvalCell]


class EvalMatrix(BaseModel):
    """The eval matrix grid: rows = cases, columns = versions (most recent first)."""

    suite: str
    versions: list[str]
    cases: list[str]
    rows: list[EvalMatrixRow]


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
