import { useEffect, useState } from "react";
import {
  getTrace,
  listTraces,
  getMetricsSummary,
  getMetricSeries,
  type RawTrace,
  type TraceTree,
  type MetricsSummary,
  type MetricSeries,
  type MetricKey,
  type Granularity,
  type MetricFilter,
} from "./client";

interface Async<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
}

// Fetch the real Langfuse trace list through the API proxy. Used only in wired
// mode; the fixture Traces list is rendered otherwise.
export function useTraces(enabled: boolean): Async<RawTrace[]> {
  const [state, setState] = useState<Async<RawTrace[]>>({ data: null, loading: enabled, error: null });
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    setState({ data: null, loading: true, error: null });
    listTraces(20)
      .then((data) => live && setState({ data, loading: false, error: null }))
      .catch((e: unknown) => live && setState({ data: null, loading: false, error: String(e) }));
    return () => {
      live = false;
    };
  }, [enabled]);
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

// Fetch a single reconstructed trace tree by id.
export function useTrace(traceId: string | null): Async<TraceTree> {
  const [state, setState] = useState<Async<TraceTree>>({ data: null, loading: !!traceId, error: null });
  useEffect(() => {
    if (!traceId) return;
    let live = true;
    setState({ data: null, loading: true, error: null });
    getTrace(traceId)
      .then((data) => live && setState({ data, loading: false, error: null }))
      .catch((e: unknown) => live && setState({ data: null, loading: false, error: String(e) }));
    return () => {
      live = false;
    };
  }, [traceId]);
  return state;
}
