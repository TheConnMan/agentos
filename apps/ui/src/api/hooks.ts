import { useQuery } from "@tanstack/react-query";
import {
  getTrace,
  listTraces,
  getMetricsSummary,
  getMetricSeries,
  getAgents,
  getCost,
  getEvalMatrix,
  listVersions,
  listDeployments,
  listAllDeployments,
  getVersionFiles,
  ApiError,
  type RawTrace,
  type TraceTree,
  type MetricsSummary,
  type MetricSeries,
  type MetricKey,
  type Granularity,
  type MetricFilter,
  type AgentOut,
  type CostReport,
  type EvalMatrix,
  type VersionOut,
  type DeploymentOut,
  type BundleFile,
} from "./client";

interface Async<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
}

// Shared mapping from a react-query result to the legacy Async<T> shape.
// `gate` is the hook's enable predicate: loading must read false whenever the
// hook is gated off, matching the pre-react-query hooks (which seeded
// `loading: enabled` / `loading: !!id` and never flipped it on while disabled).
// react-query keeps status "pending" for a disabled query, so we AND it with
// the gate rather than surfacing isPending directly. Errors keep the exact
// `String(e)` stringification the old catch handlers used, and data defaults to
// null (never undefined).
function asyncFrom<T>(
  gate: boolean,
  query: { data: T | undefined; error: unknown; isPending: boolean; isFetching: boolean },
): Async<T> {
  return {
    data: query.data ?? null,
    loading: gate && (query.isPending || query.isFetching),
    error: query.error ? String(query.error) : null,
  };
}

// Fetch the real Langfuse trace list through the API proxy. Used only in wired
// mode; the fixture Traces list is rendered otherwise.
export function useTraces(enabled: boolean, agentId?: string | null): Async<RawTrace[]> {
  const query = useQuery({
    queryKey: ["traces", agentId ?? null],
    enabled,
    queryFn: () => listTraces(20, agentId ?? undefined),
  });
  return asyncFrom(enabled, query);
}

// Fetch the metrics summary (scalar stat row) for the filter.
export function useMetricsSummary(enabled: boolean, filter: MetricFilter): Async<MetricsSummary> {
  const query = useQuery({
    queryKey: ["metricsSummary", filter],
    enabled,
    queryFn: () => getMetricsSummary(filter),
  });
  return asyncFrom(enabled, query);
}

// Fetch one metric as a time series for the chart.
export function useMetricSeries(
  enabled: boolean,
  metric: MetricKey,
  granularity: Granularity,
  filter: MetricFilter,
): Async<MetricSeries> {
  const query = useQuery({
    queryKey: ["metricSeries", metric, granularity, filter],
    enabled,
    queryFn: () => getMetricSeries(metric, granularity, filter),
  });
  return asyncFrom(enabled, query);
}

// Fetch the agent list (for the Cost view's agent selector).
export function useAgents(enabled: boolean): Async<AgentOut[]> {
  const query = useQuery({
    queryKey: ["agents"],
    enabled,
    queryFn: () => getAgents(),
  });
  return asyncFrom(enabled, query);
}

// Fetch every deployment across all agents, for env-scoping the Agents/Overview
// views. Gated on wired mode; reload happens naturally via react-query when the
// view remounts after a promote/deploy.
export function useAllDeployments(enabled: boolean): Async<DeploymentOut[]> {
  const query = useQuery({
    queryKey: ["allDeployments"],
    enabled,
    queryFn: () => listAllDeployments(),
  });
  return asyncFrom(enabled, query);
}

// Fetch the eval matrix for a suite (K1). Keyed on suite + version count so a
// suite change refetches; gated on a non-empty suite (the endpoint requires it).
export function useEvalMatrix(suite: string, versions = 5): Async<EvalMatrix> {
  const gate = suite.trim().length > 0;
  const query = useQuery({
    queryKey: ["evalMatrix", suite, versions],
    enabled: gate,
    queryFn: () => getEvalMatrix(suite, versions),
  });
  return asyncFrom(gate, query);
}

// Fetch the per-agent cost report (total + daily points).
export function useCost(agentId: string | null): Async<CostReport> {
  const query = useQuery({
    queryKey: ["cost", agentId],
    enabled: !!agentId,
    queryFn: () => getCost(agentId!),
  });
  return asyncFrom(!!agentId, query);
}

interface TraceAsync extends Async<TraceTree> {
  // True when the trace exists in the list but has no observations recorded yet
  // (API 404 "trace has no observations yet"). A legitimate empty state, not an
  // error — the caller renders an honest empty view rather than error styling.
  notFound: boolean;
}

// Fetch a single reconstructed trace tree by id.
export function useTrace(traceId: string | null): TraceAsync {
  const gate = !!traceId;
  // A 404 is resolved (not thrown) into a sentinel so it surfaces as
  // notFound=true with error===null, exactly as the old catch handler did.
  const query = useQuery({
    queryKey: ["trace", traceId],
    enabled: gate,
    queryFn: async (): Promise<{ data: TraceTree | null; notFound: boolean }> => {
      try {
        const data = await getTrace(traceId!);
        return { data, notFound: false };
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return { data: null, notFound: true };
        throw e;
      }
    },
  });
  return {
    data: query.data?.data ?? null,
    loading: gate && (query.isPending || query.isFetching),
    error: query.error ? String(query.error) : null,
    notFound: query.data?.notFound ?? false,
  };
}

// ---- FX2: agent detail — versions + active version + bundle files ----

export interface AgentVersionsData {
  versions: VersionOut[];
  deployments: DeploymentOut[];
  // The version the agent is currently serving: the most recently deployed
  // active deployment's version, else the most recently created version.
  activeVersionId: string | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
}

export function pickActiveVersion(versions: VersionOut[], deployments: DeploymentOut[]): string | null {
  // Mirror the worker's deployment resolver: prod is served first, then newest.
  // Sorting purely by recency would label a newer dev deployment "active" and
  // load the wrong version for editing while Slack traffic still runs prod.
  const ranked = deployments
    .filter((d) => d.status === "active")
    .sort((a, b) => {
      const ap = a.environment === "prod" ? 0 : 1;
      const bp = b.environment === "prod" ? 0 : 1;
      if (ap !== bp) return ap - bp;
      return b.deployed_at.localeCompare(a.deployed_at);
    });
  const chosen = ranked.find((d) => versions.some((v) => v.id === d.version_id));
  if (chosen) return chosen.version_id;
  if (versions.length === 0) return null;
  return [...versions].sort((a, b) => b.created_at.localeCompare(a.created_at))[0].id;
}

export function useAgentVersions(agentId: string | null): AgentVersionsData {
  const gate = !!agentId;
  // Keep the two-call Promise.all semantics in a single query so both lists
  // resolve together; reload() maps to refetch(), which re-runs the pair while
  // the previously fetched lists stay visible (loading flips true via isFetching
  // during the refetch, as it did when the old reloadKey re-ran the effect).
  const query = useQuery({
    queryKey: ["agentVersions", agentId],
    enabled: gate,
    queryFn: async () => {
      const [versions, deployments] = await Promise.all([listVersions(agentId!), listDeployments(agentId!)]);
      return { versions, deployments };
    },
  });
  const versions = query.data?.versions ?? [];
  const deployments = query.data?.deployments ?? [];
  const activeVersionId = pickActiveVersion(versions, deployments);
  return {
    versions,
    deployments,
    activeVersionId,
    loading: gate && (query.isPending || query.isFetching),
    error: query.error ? String(query.error) : null,
    reload: () => {
      void query.refetch();
    },
  };
}

export interface VersionFilesData {
  files: BundleFile[] | null;
  loading: boolean;
  error: string | null;
  // True on a 404: the version has no bundle stored yet (honest empty state).
  noBundle: boolean;
}

export function useVersionFiles(agentId: string | null, versionId: string | null, reloadKey = 0): VersionFilesData {
  const gate = !!(agentId && versionId);
  // reloadKey is folded into the queryKey so bumping it re-runs the fetch, the
  // same way it was an effect dependency before. A 404 resolves to a sentinel
  // (noBundle=true, error===null) instead of throwing.
  const query = useQuery({
    queryKey: ["versionFiles", agentId, versionId, reloadKey],
    enabled: gate,
    queryFn: async (): Promise<{ files: BundleFile[] | null; noBundle: boolean }> => {
      try {
        const data = await getVersionFiles(agentId!, versionId!);
        return { files: data.files, noBundle: false };
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return { files: null, noBundle: true };
        throw e;
      }
    },
  });
  return {
    files: query.data?.files ?? null,
    loading: gate && (query.isPending || query.isFetching),
    error: query.error ? String(query.error) : null,
    noBundle: query.data?.noBundle ?? false,
  };
}
