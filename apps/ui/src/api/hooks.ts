import { useEffect, useState } from "react";
import {
  getTrace,
  listTraces,
  getMetricsSummary,
  getMetricSeries,
  getAgents,
  getCost,
  listVersions,
  listDeployments,
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
  type VersionOut,
  type DeploymentOut,
  type BundleFile,
} from "./client";

interface Async<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
}

// Fetch the real Langfuse trace list through the API proxy. Used only in wired
// mode; the fixture Traces list is rendered otherwise.
export function useTraces(enabled: boolean, agentId?: string | null): Async<RawTrace[]> {
  const [state, setState] = useState<Async<RawTrace[]>>({ data: null, loading: enabled, error: null });
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    setState({ data: null, loading: true, error: null });
    listTraces(20, agentId ?? undefined)
      .then((data) => live && setState({ data, loading: false, error: null }))
      .catch((e: unknown) => live && setState({ data: null, loading: false, error: String(e) }));
    return () => {
      live = false;
    };
  }, [enabled, agentId]);
  return state;
}

// Fetch the metrics summary (scalar stat row) for the filter.
export function useMetricsSummary(enabled: boolean, filter: MetricFilter): Async<MetricsSummary> {
  const [state, setState] = useState<Async<MetricsSummary>>({ data: null, loading: enabled, error: null });
  const key = JSON.stringify(filter);
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    setState({ data: null, loading: true, error: null });
    getMetricsSummary(filter)
      .then((data) => live && setState({ data, loading: false, error: null }))
      .catch((e: unknown) => live && setState({ data: null, loading: false, error: String(e) }));
    return () => {
      live = false;
    };
    // filter is captured by value via its JSON key.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, key]);
  return state;
}

// Fetch one metric as a time series for the chart.
export function useMetricSeries(
  enabled: boolean,
  metric: MetricKey,
  granularity: Granularity,
  filter: MetricFilter,
): Async<MetricSeries> {
  const [state, setState] = useState<Async<MetricSeries>>({ data: null, loading: enabled, error: null });
  const key = JSON.stringify(filter);
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    setState({ data: null, loading: true, error: null });
    getMetricSeries(metric, granularity, filter)
      .then((data) => live && setState({ data, loading: false, error: null }))
      .catch((e: unknown) => live && setState({ data: null, loading: false, error: String(e) }));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, metric, granularity, key]);
  return state;
}

// Fetch the agent list (for the Cost view's agent selector).
export function useAgents(enabled: boolean): Async<AgentOut[]> {
  const [state, setState] = useState<Async<AgentOut[]>>({ data: null, loading: enabled, error: null });
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    setState({ data: null, loading: true, error: null });
    getAgents()
      .then((data) => live && setState({ data, loading: false, error: null }))
      .catch((e: unknown) => live && setState({ data: null, loading: false, error: String(e) }));
    return () => {
      live = false;
    };
  }, [enabled]);
  return state;
}

// Fetch the per-agent cost report (total + daily points).
export function useCost(agentId: string | null): Async<CostReport> {
  const [state, setState] = useState<Async<CostReport>>({ data: null, loading: !!agentId, error: null });
  useEffect(() => {
    if (!agentId) return;
    let live = true;
    setState({ data: null, loading: true, error: null });
    getCost(agentId)
      .then((data) => live && setState({ data, loading: false, error: null }))
      .catch((e: unknown) => live && setState({ data: null, loading: false, error: String(e) }));
    return () => {
      live = false;
    };
  }, [agentId]);
  return state;
}

interface TraceAsync extends Async<TraceTree> {
  // True when the trace exists in the list but has no observations recorded yet
  // (API 404 "trace has no observations yet"). A legitimate empty state, not an
  // error — the caller renders an honest empty view rather than error styling.
  notFound: boolean;
}

// Fetch a single reconstructed trace tree by id.
export function useTrace(traceId: string | null): TraceAsync {
  const [state, setState] = useState<TraceAsync>({ data: null, loading: !!traceId, error: null, notFound: false });
  useEffect(() => {
    if (!traceId) return;
    let live = true;
    setState({ data: null, loading: true, error: null, notFound: false });
    getTrace(traceId)
      .then((data) => live && setState({ data, loading: false, error: null, notFound: false }))
      .catch((e: unknown) => {
        if (!live) return;
        if (e instanceof ApiError && e.status === 404) {
          setState({ data: null, loading: false, error: null, notFound: true });
        } else {
          setState({ data: null, loading: false, error: String(e), notFound: false });
        }
      });
    return () => {
      live = false;
    };
  }, [traceId]);
  return state;
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
  const [versions, setVersions] = useState<VersionOut[]>([]);
  const [deployments, setDeployments] = useState<DeploymentOut[]>([]);
  const [loading, setLoading] = useState(!!agentId);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const reload = () => setReloadKey((n) => n + 1);

  useEffect(() => {
    if (!agentId) return;
    let live = true;
    setLoading(true);
    setError(null);
    Promise.all([listVersions(agentId), listDeployments(agentId)])
      .then(([vs, ds]) => {
        if (!live) return;
        setVersions(vs);
        setDeployments(ds);
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (!live) return;
        setError(String(e));
        setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [agentId, reloadKey]);

  const activeVersionId = pickActiveVersion(versions, deployments);
  return { versions, deployments, activeVersionId, loading, error, reload };
}

export interface VersionFilesData {
  files: BundleFile[] | null;
  loading: boolean;
  error: string | null;
  // True on a 404: the version has no bundle stored yet (honest empty state).
  noBundle: boolean;
}

export function useVersionFiles(agentId: string | null, versionId: string | null, reloadKey = 0): VersionFilesData {
  const [state, setState] = useState<VersionFilesData>({
    files: null,
    loading: !!(agentId && versionId),
    error: null,
    noBundle: false,
  });
  useEffect(() => {
    if (!agentId || !versionId) return;
    let live = true;
    setState({ files: null, loading: true, error: null, noBundle: false });
    getVersionFiles(agentId, versionId)
      .then((data) => live && setState({ files: data.files, loading: false, error: null, noBundle: false }))
      .catch((e: unknown) => {
        if (!live) return;
        if (e instanceof ApiError && e.status === 404) {
          setState({ files: null, loading: false, error: null, noBundle: true });
        } else {
          setState({ files: null, loading: false, error: String(e), noBundle: false });
        }
      });
    return () => {
      live = false;
    };
  }, [agentId, versionId, reloadKey]);
  return state;
}
