import { useEffect, useState } from "react";
import { getTrace, listTraces, type RawTrace, type TraceTree } from "./client";

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
