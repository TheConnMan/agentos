// Typed client for the B1/B2 API, reached through the same-origin /api proxy.
// Shapes mirror apps/api/openapi.json.
//
// Authentication is the console session cookie and nothing else (#630 /
// ADR-0049): the cookie is HttpOnly, so this module cannot read it and never
// tries to. Every call opts into sending it with `credentials: "same-origin"`.
// No call carries the platform key -- it has no browser-reachable path, so
// there is no key here to attach.

import { API_PREFIX } from "./config";

// Open (unauthenticated) app config: the configurable org/workspace name.
export interface AppConfig {
  org_name: string;
}

export interface AgentOut {
  id: string;
  name: string;
  slack_channel: string;
  // Per-agent model id, forwarded as AGENTOS_MODEL at boot (#254). null uses the
  // platform default model.
  model: string | null;
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

// Spread into every request init so the HttpOnly session cookie rides along.
// The API is strictly same-origin (it runs no CORS middleware on purpose), so
// "same-origin" is both the correct scope and the tightest one.
const SAME_ORIGIN = { credentials: "same-origin" } as const;

const JSON_HEADERS = { "Content-Type": "application/json" };

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

// ---- console session (#630 / ADR-0049) ----

/** The console's own auth state, as the server sees it. */
export interface ConsoleSession {
  authenticated: boolean;
  expires_at: string | null;
}

/**
 * Read the current console session. A 401 is the expected answer for a console
 * that has not logged in yet, so it resolves to an unauthenticated session
 * rather than throwing: being logged out is a state the gate renders, not an
 * error it has to recover from. Any other failure still throws ApiError.
 */
export async function getSession(): Promise<ConsoleSession> {
  const resp = await fetch(url("/console/session"), { ...SAME_ORIGIN });
  if (resp.status === 401) return { authenticated: false, expires_at: null };
  return jsonOrThrow<ConsoleSession>(resp);
}

/**
 * Exchange a single-use login code minted by `agentos <local|cluster> console
 * login` for a session. The server consumes the code and returns the session as
 * an HttpOnly cookie, so the code is spent here and never stored: this module
 * keeps it only as the argument of this call. A rejected or expired code
 * surfaces as ApiError carrying the server's reason.
 */
export async function activateSession(code: string): Promise<ConsoleSession> {
  const resp = await fetch(url("/console/session"), {
    method: "POST",
    ...SAME_ORIGIN,
    headers: JSON_HEADERS,
    body: JSON.stringify({ code }),
  });
  return jsonOrThrow<ConsoleSession>(resp);
}

/** Revoke the current session server-side (the row is marked, not deleted). */
export async function logout(): Promise<void> {
  const resp = await fetch(url("/console/session"), { method: "DELETE", ...SAME_ORIGIN });
  if (resp.ok) return;
  const body = await resp.json().catch(() => null);
  throw new ApiError(resp.status, describeError(body) ?? resp.statusText);
}

export async function createAgent(input: {
  name: string;
  slack_channel: string;
  // Optional per-agent model id (#254). Omit for the platform default.
  model?: string;
}): Promise<AgentOut> {
  const resp = await fetch(url("/agents"), {
    method: "POST",
    ...SAME_ORIGIN,
    headers: JSON_HEADERS,
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
    ...SAME_ORIGIN,
    headers: JSON_HEADERS,
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
    ...SAME_ORIGIN,
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
    ...SAME_ORIGIN,
  });
  return jsonOrThrow<RawTrace[]>(resp);
}

export async function getTrace(traceId: string): Promise<TraceTree> {
  const resp = await fetch(url(`/langfuse/traces/${encodeURIComponent(traceId)}`), {
    ...SAME_ORIGIN,
  });
  return jsonOrThrow<TraceTree>(resp);
}

// Promote a trace into an anonymized, runnable eval case (#259). The API reads
// the trace, scrubs PII, and returns a case in the frozen eval-case format.
export async function promoteTraceToEvalCase(traceId: string): Promise<EvalCaseOut> {
  const resp = await fetch(url(`/langfuse/traces/${encodeURIComponent(traceId)}/eval-case`), {
    method: "POST",
    ...SAME_ORIGIN,
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
    ...SAME_ORIGIN,
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
    ...SAME_ORIGIN,
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
    { ...SAME_ORIGIN },
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
    { ...SAME_ORIGIN },
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
  const resp = await fetch(url("/agents"), { ...SAME_ORIGIN });
  return jsonOrThrow<AgentOut[]>(resp);
}

// The open /config endpoint (no API key required) carries the configurable
// org/workspace name the shared chrome renders.
export async function getConfig(): Promise<AppConfig> {
  const resp = await fetch(url("/config"), { ...SAME_ORIGIN });
  return jsonOrThrow<AppConfig>(resp);
}

// PATCH an agent's mutable fields (currently just its Slack channel). Returns
// the updated agent. Mirrors createAgent's JSON-body shape; non-2xx throws
// ApiError. The live worker keeps its channel until the next deploy — this only
// updates the stored config the next deployment reads.
export async function updateAgent(
  agentId: string,
  patch: { slack_channel?: string; model?: string },
): Promise<AgentOut> {
  const resp = await fetch(url(`/agents/${agentId}`), {
    method: "PATCH",
    ...SAME_ORIGIN,
    headers: JSON_HEADERS,
    body: JSON.stringify(patch),
  });
  return jsonOrThrow<AgentOut>(resp);
}

// Delete an agent (cascades its versions/deployments server-side; 204 No Content
// on success). A 409 (active deployment) surfaces via the thrown ApiError.
export async function deleteAgent(agentId: string): Promise<void> {
  const resp = await fetch(url(`/agents/${agentId}`), { method: "DELETE", ...SAME_ORIGIN });
  if (resp.ok) return;
  const body = await resp.json().catch(() => null);
  throw new ApiError(resp.status, describeError(body) ?? resp.statusText);
}

export async function getCost(agentId: string, range: { start?: string; end?: string } = {}): Promise<CostReport> {
  const resp = await fetch(url(`/agents/${agentId}/cost${query({ ...range })}`), { ...SAME_ORIGIN });
  return jsonOrThrow<CostReport>(resp);
}

export async function getBudget(agentId: string): Promise<BudgetConfig> {
  const resp = await fetch(url(`/agents/${agentId}/budget`), { ...SAME_ORIGIN });
  return jsonOrThrow<BudgetConfig>(resp);
}

// PUT the budget. A non-positive value 422s server-side (Field(gt=0)); the
// ApiError message carries the field-level reason for inline display.
export async function putBudget(agentId: string, budget: BudgetConfig): Promise<BudgetConfig> {
  const resp = await fetch(url(`/agents/${agentId}/budget`), {
    method: "PUT",
    ...SAME_ORIGIN,
    headers: JSON_HEADERS,
    body: JSON.stringify(budget),
  });
  return jsonOrThrow<BudgetConfig>(resp);
}

export async function getKillState(agentId: string): Promise<KillState> {
  const resp = await fetch(url(`/agents/${agentId}/kill`), { ...SAME_ORIGIN });
  return jsonOrThrow<KillState>(resp);
}

// ---- FX2: agent detail — versions, bundle files, and version activation ----

export type Environment = "prod" | "dev";

export interface DeploymentOut {
  id: string;
  agent_id: string;
  version_id: string;
  environment: Environment;
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
  const resp = await fetch(url(`/agents/${agentId}/versions`), { ...SAME_ORIGIN });
  return jsonOrThrow<VersionOut[]>(resp);
}

export async function listDeployments(agentId: string): Promise<DeploymentOut[]> {
  const resp = await fetch(url(`/deployments${query({ agent_id: agentId })}`), { ...SAME_ORIGIN });
  return jsonOrThrow<DeploymentOut[]>(resp);
}

// Every deployment across all agents (GET /deployments with no agent_id). Used
// by the env-scoped Agents/Overview views to decide which agents are live in the
// selected environment.
export async function listAllDeployments(): Promise<DeploymentOut[]> {
  const resp = await fetch(url("/deployments"), { ...SAME_ORIGIN });
  return jsonOrThrow<DeploymentOut[]>(resp);
}

/**
 * Read the unwrapped files of a version's stored bundle (FX2 headline: the agent
 * detail surface renders and edits these). A 404 means no bundle is stored for
 * the version yet; callers distinguish it via the thrown ApiError's status.
 */
export async function getVersionFiles(agentId: string, versionId: string): Promise<BundleFiles> {
  const resp = await fetch(url(`/agents/${agentId}/versions/${versionId}/files`), { ...SAME_ORIGIN });
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
    ...SAME_ORIGIN,
    headers: JSON_HEADERS,
    body: JSON.stringify(input),
  });
  return jsonOrThrow<DeploymentOut>(resp);
}

// ---- Agent memory: inspect / edit / delete learned memory (#267) ----

// Where a memory entry was learned from (#264 Provenance shape). Provenance is
// the differentiator: an operator can see which session/traces taught a lesson.
export interface MemoryProvenance {
  learned_from_session_id: string | null;
  source_trace_ids: string[];
  recorded_at: string;
}

// One learned memory entry. `index` is its position in the append only memory
// log. It is valid only with the accompanying parent log version.
export interface MemoryEntry {
  index: number;
  version: number;
  content: string;
  provenance: MemoryProvenance;
}

// List an agent's learned memory, oldest first (empty for a fresh agent).
export async function listMemory(agentId: string): Promise<MemoryEntry[]> {
  const resp = await fetch(url(`/agents/${agentId}/memory`), { ...SAME_ORIGIN });
  return jsonOrThrow<MemoryEntry[]>(resp);
}

// Edit one entry's content in place; the server preserves its provenance. The
// change is reflected at the agent's next session boot (it rehydrates the log).
export async function editMemory(
  agentId: string,
  index: number,
  content: string,
  expectedVersion: number,
): Promise<MemoryEntry> {
  const resp = await fetch(url(`/agents/${agentId}/memory/${index}`), {
    method: "PUT",
    ...SAME_ORIGIN,
    headers: JSON_HEADERS,
    body: JSON.stringify({ content, expected_version: expectedVersion }),
  });
  return jsonOrThrow<MemoryEntry>(resp);
}

// Delete exactly one memory entry (204 on success). Remaining entries keep order.
export async function deleteMemory(
  agentId: string,
  index: number,
  expectedVersion: number,
): Promise<void> {
  const resp = await fetch(
    url(`/agents/${agentId}/memory/${index}${query({ expected_version: expectedVersion })}`),
    {
      method: "DELETE",
      ...SAME_ORIGIN,
    },
  );
  if (resp.ok) return;
  const body = await resp.json().catch(() => null);
  throw new ApiError(resp.status, describeError(body) ?? resp.statusText);
}

export async function killAgent(agentId: string): Promise<KillState> {
  const resp = await fetch(url(`/agents/${agentId}/kill`), { method: "POST", ...SAME_ORIGIN });
  return jsonOrThrow<KillState>(resp);
}

export async function resumeAgent(agentId: string): Promise<KillState> {
  const resp = await fetch(url(`/agents/${agentId}/resume`), { method: "POST", ...SAME_ORIGIN });
  return jsonOrThrow<KillState>(resp);
}
