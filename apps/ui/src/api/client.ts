// Typed client for the B1/B2 API, reached through the same-origin /api proxy.
// Every call carries the X-API-Key header. Shapes mirror apps/api/openapi.json.

import { API_PREFIX, apiKey } from "./config";

// Open (unauthenticated) app config: the configurable org/workspace name.
export interface AppConfig {
  org_name: string;
}

export interface AgentOut {
  id: string;
  name: string;
  slack_channel: string;
  created_at: string;
}

export interface VersionOut {
  id: string;
  agent_id: string;
  version_label: string;
  bundle_ref: string | null;
  bundle_sha256: string | null;
  created_by: string;
  created_at: string;
}

export interface BundleOut {
  version_id: string;
  bundle_ref: string;
  bundle_sha256: string;
  size_bytes: number;
}

// One issue from the frozen plugin_format validator, surfaced on a 422.
export interface BundleIssue {
  code: string;
  message: string;
  location: string;
}

export interface ObservationNode {
  id: string;
  type: string;
  name: string | null;
  model: string | null;
  startTime: string | null;
  usageDetails: Record<string, unknown> | null;
  children: ObservationNode[];
}

export interface TraceTree {
  trace: Record<string, unknown>;
  tree: ObservationNode[];
  // The serving sandbox id (agentos.sandbox_id), hoisted server-side from the
  // trace/observation resource attributes; null when the trace predates it.
  sandbox_id: string | null;
}

// A raw Langfuse trace row (opaque; we read a few well-known fields defensively).
export type RawTrace = Record<string, unknown>;

// An eval case in the frozen eval-case format (#8/#259): an input prompt plus a
// deterministic grader. Returned by promoteTraceToEvalCase.
export interface GraderOut {
  kind: "exact" | "contains" | "regex";
  expected: string;
  case_sensitive: boolean;
}
export interface EvalCaseOut {
  id: string;
  input: string;
  grader: GraderOut;
}

/** Thrown when the bundle validator rejects the archive (HTTP 422). */
export class BundleValidationError extends Error {
  issues: BundleIssue[];
  constructor(issues: BundleIssue[]) {
    super("bundle failed validation");
    this.name = "BundleValidationError";
    this.issues = issues;
  }
}

/** Thrown for any other non-2xx response. */
export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function url(path: string): string {
  return `${API_PREFIX}${path}`;
}

function headers(extra?: Record<string, string>): Record<string, string> {
  return { "X-API-Key": apiKey(), ...extra };
}

async function jsonOrThrow<T>(resp: Response): Promise<T> {
  if (resp.ok) return (await resp.json()) as T;
  const body = await resp.json().catch(() => null);
  throw new ApiError(resp.status, describeError(body) ?? resp.statusText);
}

function describeError(body: unknown): string | null {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object" && "detail" in detail) {
      const inner = (detail as { detail: unknown }).detail;
      if (typeof inner === "string") return inner;
    }
    // FastAPI field-validation errors: detail is an array of {loc, msg, type}.
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { loc?: unknown[]; msg?: unknown };
      const field = Array.isArray(first.loc) ? first.loc[first.loc.length - 1] : undefined;
      const msg = typeof first.msg === "string" ? first.msg : "invalid value";
      return field ? `${field}: ${msg}` : msg;
    }
  }
  return null;
}

export async function createAgent(input: { name: string; slack_channel: string }): Promise<AgentOut> {
  const resp = await fetch(url("/agents"), {
    method: "POST",
    headers: headers({ "Content-Type": "application/json" }),
    body: JSON.stringify(input),
  });
  return jsonOrThrow<AgentOut>(resp);
}

export async function createVersion(
  agentId: string,
  input: { version_label: string; created_by: string },
): Promise<VersionOut> {
  const resp = await fetch(url(`/agents/${agentId}/versions`), {
    method: "POST",
    headers: headers({ "Content-Type": "application/json" }),
    body: JSON.stringify(input),
  });
  return jsonOrThrow<VersionOut>(resp);
}

/**
 * PUT the bundle archive as multipart/form-data. On 422 the plugin validator's
 * issues are extracted and thrown as BundleValidationError so the editor can
 * render them inline; any other failure throws ApiError.
 */
export async function uploadBundle(
  agentId: string,
  versionId: string,
  archive: Blob,
): Promise<BundleOut> {
  const form = new FormData();
  form.append("file", archive, "bundle.zip");
  const resp = await fetch(url(`/agents/${agentId}/versions/${versionId}/bundle`), {
    method: "PUT",
    headers: headers(),
    body: form,
  });
  if (resp.ok) return (await resp.json()) as BundleOut;
  const body = await resp.json().catch(() => null);
  const issues = extractIssues(body);
  if (resp.status === 422 && issues) throw new BundleValidationError(issues);
  throw new ApiError(resp.status, describeError(body) ?? resp.statusText);
}

// The bundle 422 body is { detail: { detail: "...", errors: [ {code,message,location} ] } }.
function extractIssues(body: unknown): BundleIssue[] | null {
  if (!body || typeof body !== "object" || !("detail" in body)) return null;
  const detail = (body as { detail: unknown }).detail;
  if (!detail || typeof detail !== "object" || !("errors" in detail)) return null;
  const errors = (detail as { errors: unknown }).errors;
  if (!Array.isArray(errors)) return null;
  return errors.map((e) => {
    const o = (e ?? {}) as Record<string, unknown>;
    return {
      code: String(o.code ?? "unknown"),
      message: String(o.message ?? ""),
      location: String(o.location ?? ""),
    };
  });
}

// List recent traces. With agentId, the API filters to that agent's runs (its
// traces carry the `agent-<id>` name token); without it, all recent traces.
export async function listTraces(limit = 20, agentId?: string): Promise<RawTrace[]> {
  const resp = await fetch(url(`/langfuse/traces${query({ limit, agent_id: agentId })}`), {
    headers: headers(),
  });
  return jsonOrThrow<RawTrace[]>(resp);
}

export async function getTrace(traceId: string): Promise<TraceTree> {
  const resp = await fetch(url(`/langfuse/traces/${encodeURIComponent(traceId)}`), {
    headers: headers(),
  });
  return jsonOrThrow<TraceTree>(resp);
}

// Promote a trace into an anonymized, runnable eval case (#259). The API reads
// the trace, scrubs PII, and returns a case in the frozen eval-case format.
export async function promoteTraceToEvalCase(traceId: string): Promise<EvalCaseOut> {
  const resp = await fetch(url(`/langfuse/traces/${encodeURIComponent(traceId)}/eval-case`), {
    method: "POST",
    headers: headers(),
  });
  return jsonOrThrow<EvalCaseOut>(resp);
}

// ---- observability (OB1): Langfuse-backed metrics + runner-pod log proxy ----

export type MetricKey = "runs" | "latency_p95_ms" | "tokens" | "cost_usd" | "error_rate";
export type Granularity = "hour" | "day" | "week";

export interface MetricsSummary {
  start: string;
  end: string;
  runs: number;
  latency_p95_ms: number;
  tokens: number;
  cost_usd: number;
  error_rate: number;
}

export interface MetricPoint {
  ts: string;
  value: number;
}

export interface MetricSeries {
  metric: string;
  granularity: string;
  start: string;
  end: string;
  points: MetricPoint[];
}

export interface PodLogs {
  namespace: string;
  pod: string;
  container: string | null;
  logs: string;
}

export interface RunnerPods {
  namespace: string;
  pods: string[];
}

// List the runner sandbox pods in a namespace (populates the Logs dropdown).
// Non-2xx throws ApiError carrying the status: 503 (no cluster), 502 (other).
export async function listRunnerPods(namespace?: string): Promise<RunnerPods> {
  const resp = await fetch(url(`/observability/runners${query({ namespace })}`), {
    headers: headers(),
  });
  return jsonOrThrow<RunnerPods>(resp);
}

// The per-agent filter is a trace-name substring server-side, so it is passed as
// a plain `agent` query param, not a promise of exact matching.
export interface MetricFilter {
  environment?: string;
  agent?: string;
}

function query(params: Record<string, string | number | boolean | undefined>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "") q.set(k, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

export async function getMetricsSummary(filter: MetricFilter = {}): Promise<MetricsSummary> {
  const resp = await fetch(url(`/observability/metrics/summary${query({ ...filter })}`), {
    headers: headers(),
  });
  return jsonOrThrow<MetricsSummary>(resp);
}

export async function getMetricSeries(
  metric: MetricKey,
  granularity: Granularity,
  filter: MetricFilter = {},
): Promise<MetricSeries> {
  const resp = await fetch(
    url(`/observability/metrics/series${query({ metric, granularity, ...filter })}`),
    { headers: headers() },
  );
  return jsonOrThrow<MetricSeries>(resp);
}

export interface RunnerLogsQuery {
  container?: string;
  tail_lines?: number;
  previous?: boolean;
}

// Fetch runner-pod logs. Non-2xx throws ApiError carrying the status so the view
// can render distinct states: 503 (no cluster), 404 (missing pod), 502 (other).
export async function getRunnerLogs(
  namespace: string,
  pod: string,
  opts: RunnerLogsQuery = {},
): Promise<PodLogs> {
  const resp = await fetch(
    url(
      `/observability/runners/${encodeURIComponent(namespace)}/${encodeURIComponent(pod)}/logs${query({ ...opts })}`,
    ),
    { headers: headers() },
  );
  return jsonOrThrow<PodLogs>(resp);
}

// ---- L1: per-agent cost, budget, and the kill switch ----

export interface CostReport {
  start: string;
  end: string;
  total_usd: number;
  points: MetricPoint[];
}

// Both fields nullable; null means platform defaults. Values must be > 0.
export interface BudgetConfig {
  max_usd_per_day: number | null;
  max_output_tokens_per_run: number | null;
}

export interface KillState {
  killed: boolean;
}

export async function getAgents(): Promise<AgentOut[]> {
  const resp = await fetch(url("/agents"), { headers: headers() });
  return jsonOrThrow<AgentOut[]>(resp);
}

// The open /config endpoint (no API key required) carries the configurable
// org/workspace name the shared chrome renders.
export async function getConfig(): Promise<AppConfig> {
  const resp = await fetch(url("/config"));
  return jsonOrThrow<AppConfig>(resp);
}

// PATCH an agent's mutable fields (currently just its Slack channel). Returns
// the updated agent. Mirrors createAgent's JSON-body shape; non-2xx throws
// ApiError. The live worker keeps its channel until the next deploy — this only
// updates the stored config the next deployment reads.
export async function updateAgent(agentId: string, patch: { slack_channel: string }): Promise<AgentOut> {
  const resp = await fetch(url(`/agents/${agentId}`), {
    method: "PATCH",
    headers: headers({ "Content-Type": "application/json" }),
    body: JSON.stringify(patch),
  });
  return jsonOrThrow<AgentOut>(resp);
}

// Delete an agent (cascades its versions/deployments server-side; 204 No Content
// on success). A 409 (active deployment) surfaces via the thrown ApiError.
export async function deleteAgent(agentId: string): Promise<void> {
  const resp = await fetch(url(`/agents/${agentId}`), { method: "DELETE", headers: headers() });
  if (resp.ok) return;
  const body = await resp.json().catch(() => null);
  throw new ApiError(resp.status, describeError(body) ?? resp.statusText);
}

export async function getCost(agentId: string, range: { start?: string; end?: string } = {}): Promise<CostReport> {
  const resp = await fetch(url(`/agents/${agentId}/cost${query({ ...range })}`), { headers: headers() });
  return jsonOrThrow<CostReport>(resp);
}

export async function getBudget(agentId: string): Promise<BudgetConfig> {
  const resp = await fetch(url(`/agents/${agentId}/budget`), { headers: headers() });
  return jsonOrThrow<BudgetConfig>(resp);
}

// PUT the budget. A non-positive value 422s server-side (Field(gt=0)); the
// ApiError message carries the field-level reason for inline display.
export async function putBudget(agentId: string, budget: BudgetConfig): Promise<BudgetConfig> {
  const resp = await fetch(url(`/agents/${agentId}/budget`), {
    method: "PUT",
    headers: headers({ "Content-Type": "application/json" }),
    body: JSON.stringify(budget),
  });
  return jsonOrThrow<BudgetConfig>(resp);
}

export async function getKillState(agentId: string): Promise<KillState> {
  const resp = await fetch(url(`/agents/${agentId}/kill`), { headers: headers() });
  return jsonOrThrow<KillState>(resp);
}

// ---- FX2: agent detail — versions, bundle files, and version activation ----

export type Environment = "prod" | "dev";

export interface DeploymentOut {
  id: string;
  agent_id: string;
  version_id: string;
  environment: Environment;
  bot_identity: string | null;
  commit_sha: string | null;
  status: string;
  deployed_at: string;
}

// One unwrapped file from a stored bundle. `path` is bundle-root-relative, e.g.
// "skills/deal-desk/SKILL.md" or ".claude-plugin/plugin.json".
export interface BundleFile {
  path: string;
  content: string;
}

export interface BundleFiles {
  files: BundleFile[];
}

export async function listVersions(agentId: string): Promise<VersionOut[]> {
  const resp = await fetch(url(`/agents/${agentId}/versions`), { headers: headers() });
  return jsonOrThrow<VersionOut[]>(resp);
}

export async function listDeployments(agentId: string): Promise<DeploymentOut[]> {
  const resp = await fetch(url(`/deployments${query({ agent_id: agentId })}`), { headers: headers() });
  return jsonOrThrow<DeploymentOut[]>(resp);
}

// Every deployment across all agents (GET /deployments with no agent_id). Used
// by the env-scoped Agents/Overview views to decide which agents are live in the
// selected environment.
export async function listAllDeployments(): Promise<DeploymentOut[]> {
  const resp = await fetch(url("/deployments"), { headers: headers() });
  return jsonOrThrow<DeploymentOut[]>(resp);
}

/**
 * Read the unwrapped files of a version's stored bundle (FX2 headline: the agent
 * detail surface renders and edits these). A 404 means no bundle is stored for
 * the version yet; callers distinguish it via the thrown ApiError's status.
 */
export async function getVersionFiles(agentId: string, versionId: string): Promise<BundleFiles> {
  const resp = await fetch(url(`/agents/${agentId}/versions/${versionId}/files`), { headers: headers() });
  return jsonOrThrow<BundleFiles>(resp);
}

export type DeploymentStatus = "active" | "inactive";

export interface DeploymentCreate {
  agent_id: string;
  version_id: string;
  environment: Environment;
  // Optional; the API's DeploymentCreate defaults to "active" server-side. The
  // rollback path sets it explicitly to redeploy an old version as active.
  status?: DeploymentStatus;
}

// Activate a version by creating a deployment for it (status defaults to active
// server-side). This is the third step of the redeploy sequence, after POST
// version + PUT bundle.
export async function createDeployment(input: DeploymentCreate): Promise<DeploymentOut> {
  const resp = await fetch(url("/deployments"), {
    method: "POST",
    headers: headers({ "Content-Type": "application/json" }),
    body: JSON.stringify(input),
  });
  return jsonOrThrow<DeploymentOut>(resp);
}

export async function killAgent(agentId: string): Promise<KillState> {
  const resp = await fetch(url(`/agents/${agentId}/kill`), { method: "POST", headers: headers() });
  return jsonOrThrow<KillState>(resp);
}

export async function resumeAgent(agentId: string): Promise<KillState> {
  const resp = await fetch(url(`/agents/${agentId}/resume`), { method: "POST", headers: headers() });
  return jsonOrThrow<KillState>(resp);
}
