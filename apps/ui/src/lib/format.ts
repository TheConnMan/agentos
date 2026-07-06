// The OB1 metrics API sources latency from Langfuse's metrics `latency` measure,
// which is reported in MILLISECONDS despite the `latency_p95_seconds` field name
// (the Langfuse trace object's own `latency` is seconds, but the aggregate
// metrics measure is ms). Formatting that value directly as seconds overstated
// latency by 1000x. Convert here, in one place, so every display reads honest
// units: sub-second values as ms, larger values as seconds.
export function formatLatency(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}
