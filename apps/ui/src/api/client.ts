// Typed client for the B1/B2 API, reached through the same-origin /api proxy.
// Every call carries the X-API-Key header. Shapes mirror apps/api/openapi.json.

import { API_PREFIX, apiKey } from "./config";

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
}

// A raw Langfuse trace row (opaque; we read a few well-known fields defensively).
export type RawTrace = Record<string, unknown>;

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

export async function listTraces(limit = 20): Promise<RawTrace[]> {
  const resp = await fetch(url(`/langfuse/traces?limit=${limit}`), { headers: headers() });
  return jsonOrThrow<RawTrace[]>(resp);
}

export async function getTrace(traceId: string): Promise<TraceTree> {
  const resp = await fetch(url(`/langfuse/traces/${encodeURIComponent(traceId)}`), {
    headers: headers(),
  });
  return jsonOrThrow<TraceTree>(resp);
}
