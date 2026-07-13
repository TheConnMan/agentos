import type { TraceSummary, TraceSpan, FixtureLevel } from "./index";

const OK: TraceSummary = {
  id: "tr_8f3k21",
  msg: "@agentos can we approve the Meridian deal at 18% discount?",
  agent: "deal-desk",
  dur: "2.1s",
  tools: 3,
  cost: "$0.04",
  tokens: "4.2k",
  status: "ok",
  when: "2 min ago",
};

const FAIL: TraceSummary = {
  id: "tr_2a91cd",
  msg: "@agentos approve Northwind at 22%?",
  agent: "deal-desk",
  dur: "1.8s",
  tools: 2,
  cost: "$0.03",
  tokens: "3.1k",
  status: "fail",
  when: "Tue 14:02",
};

// The failed trace only exists once CI is connected (level 4+), matching the
// canon's traces() which shows just the happy path before then.
export function tracesForLevel(level: FixtureLevel): TraceSummary[] {
  if (level < 4) return [OK];
  return [FAIL, OK];
}

// Build the span waterfall for a trace as frozen ACI-protocol events: the
// inbound Slack message (Event), the tool call (ToolNote), and the terminal
// Final (ok) or ErrorEvent (fail). The UI renders the drill-in from these.
export function traceSpans(t: TraceSummary): TraceSpan[] {
  const bad = t.status === "fail";
  const dealId = bad ? "NW-2231" : "MER-8841";
  return [
    {
      label: "Slack message",
      detail: t.msg,
      offset: "0ms",
      kind: "message",
      event: { kind: "event", type: "message", text: t.msg, ts: "2026-07-05T14:01:00Z", user: bad ? "priya" : "mara" },
    },
    {
      label: "Skill invoked",
      detail: "deal-desk · skill.md",
      offset: "+12ms",
      kind: "skill",
      event: { type: "tool_note", version: "0.2.0", text: "matched skill deal-desk", tool: "router" },
    },
    {
      label: "Tool call",
      detail: `salesforce.get_deal(id: ${dealId})`,
      offset: "+840ms",
      kind: "tool",
      event: { type: "tool_note", version: "0.2.0", text: `salesforce.get_deal(id: ${dealId})`, tool: "salesforce.get_deal" },
    },
    bad
      ? {
          label: "Response",
          detail: "approver ‘Dana’ not found in policy.yaml — hallucinated value",
          offset: "+1.8s",
          kind: "response",
          bad: true,
          event: { type: "error", version: "0.2.0", message: "approver 'Dana' not found in policy.yaml", classification: "hallucinated-value" },
        }
      : {
          label: "Response",
          detail: "Verdict returned · routed to J. Whitfield",
          offset: "+2.1s",
          kind: "response",
          event: { type: "final", version: "0.2.0", text: "Verdict returned · routed to J. Whitfield", status: "done" },
        },
  ];
}
