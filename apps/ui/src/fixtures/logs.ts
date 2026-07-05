import type { LogLine } from "./types";

// Loki-style structured log stream. The error line only appears once CI is on
// (level 4+), matching the canon's obsLogs() gate.
export function logsForLevel(ghOn: boolean): LogLine[] {
  const lines: (LogLine | null)[] = [
    { ts: "14:02:07.214", level: "info", msg: "request received", fields: { user: "mara", msg_len: 52 } },
    { ts: "14:02:07.226", level: "info", msg: "skill matched deal-desk", fields: { score: 0.98 } },
    { ts: "14:02:07.240", level: "info", msg: "tool salesforce.get_deal", fields: { id: "MER-8841", ms: 812 } },
    { ts: "14:02:08.061", level: "info", msg: "policy check discount<=15", fields: { requested: 18, result: "route" } },
    { ts: "14:02:08.079", level: "info", msg: "verdict routed to J. Whitfield", fields: { ms: 2100, cost: "$0.04" } },
    { ts: "14:03:41.882", level: "warn", msg: "latency above p95 target", fields: { ms: 5210, agent: "rev-analytics" } },
    { ts: "14:05:12.004", level: "info", msg: "tool datadog.query", fields: { ms: 430 } },
    ghOn ? { ts: "14:07:55.610", level: "error", msg: "approver not found in policy.yaml", fields: { value: "Dana", trace: "tr_2a91cd" } } : null,
    { ts: "14:09:03.117", level: "info", msg: "request received", fields: { user: "jt", msg_len: 34 } },
    { ts: "14:09:04.559", level: "info", msg: "verdict auto-approved", fields: { discount: 12, ms: 1420 } },
  ];
  return lines.filter((l): l is LogLine => l !== null);
}
