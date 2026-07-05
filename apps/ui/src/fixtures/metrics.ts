// Deterministic time-series generator ported verbatim from the canon's obsMetrics
// gen(): a base plus linear trend plus two sine components. No randomness, so
// charts render identically every run (important for Playwright screenshots).
export function genSeries(base: number, amp: number, trend = 0, n = 32): number[] {
  return Array.from({ length: n }, (_, i) =>
    Math.max(
      0,
      +(base + trend * i + amp * Math.sin(i / 2.3) + amp * 0.5 * Math.sin(i / 1.05 + 1)).toFixed(2),
    ),
  );
}

export interface MetricPanel {
  title: string;
  value: string;
  unit: string;
  series: number[];
  color: "brand" | "warn" | "link" | "mutedStatus";
}

export const METRIC_PANELS: MetricPanel[] = [
  { title: "Error rate", value: "1.8", unit: "%", series: genSeries(1.4, 0.9, -0.01), color: "warn" },
  { title: "Latency p95", value: "2.1", unit: "s", series: genSeries(1.9, 0.4, 0), color: "brand" },
  { title: "Latency p50", value: "0.9", unit: "s", series: genSeries(0.8, 0.2, 0), color: "brand" },
  { title: "Tokens / min", value: "4.2", unit: "k", series: genSeries(4, 1.1, 0.02), color: "link" },
  { title: "Tool calls / req", value: "3.1", unit: "", series: genSeries(3, 0.7, 0), color: "mutedStatus" },
  { title: "Active sessions", value: "12", unit: "", series: genSeries(9, 3, 0), color: "brand" },
];

export const REQUEST_RATE_SERIES = genSeries(6, 2.2, 0.03);

// Usage view: top users and intents differ between single-agent and fleet views.
export const USAGE_SINGLE = {
  users: [["mara", 8], ["jt", 5], ["priya", 3]] as [string, number][],
  intents: [["approve deal", 12], ["route to approver", 4]] as [string, number][],
};

export const USAGE_FLEET = {
  users: [["mara", 142], ["priya", 98], ["jt", 76], ["sam", 54], ["others", 415]] as [string, number][],
  intents: [
    ["approve deal", 188],
    ["why did checkout error spike", 121],
    ["MRR by segment", 96],
    ["who owns incident", 77],
  ] as [string, number][],
  overrides: [["deal-desk", "4%"], ["rev-analytics", "11%"], ["sre-triage", "2%"]] as [string, string][],
};

// Drift chart: 30-day eval pass-rate for rev-analytics with deploy markers.
export const DRIFT_SERIES = [
  96, 96, 95, 96, 95, 95, 96, 95, 94, 95, 94, 93, 94, 93, 92, 92, 90, 88, 89, 87, 86, 85, 84, 84, 83, 83, 82, 82, 83, 82,
];

export const DRIFT_MARKERS = [
  { i: 4, label: "v2.0.4", bad: false },
  { i: 16, label: "v2.1.0 · model update", bad: true },
  { i: 26, label: "v2.1.1", bad: false },
];
