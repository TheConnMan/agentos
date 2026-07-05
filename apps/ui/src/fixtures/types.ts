import type { Event, ToolNote, Final, ErrorEvent } from "@aci/aci-protocol";

export type Health = "green" | "amber" | "red";

export interface Agent {
  id: string;
  ch: string;
  prodV: string;
  devV: string;
  plugin?: boolean;
  score: number;
  runs: number;
  cost: string;
  health: Health;
  /** 10-point eval-score history for the inline sparkline. */
  trend: number[];
}

export interface EvalCase {
  n: string;
  /** pass (1) / fail (0) per version column, in matrix order. */
  s: [number, number, number, number];
  dur?: string;
}

export type TraceStatus = "ok" | "fail";

export interface TraceSummary {
  id: string;
  msg: string;
  agent: string;
  dur: string;
  tools: number;
  cost: string;
  tokens: string;
  status: TraceStatus;
  when: string;
}

/**
 * A single row in the trace-detail span waterfall. The `event` carries the
 * frozen ACI-protocol shape for the span (inbound Event, ToolNote, Final, or
 * ErrorEvent) so the UI renders against the same contract the runner emits.
 */
export interface TraceSpan {
  label: string;
  detail: string;
  offset: string;
  kind: "message" | "skill" | "tool" | "response";
  bad?: boolean;
  event: Event | ToolNote | Final | ErrorEvent;
}

export type VersionState = "production" | "preview" | "failed";

export interface VersionRow {
  branch: "main" | "dev";
  ver: string;
  dep: string;
  score: string;
  by: string;
  human: boolean;
  status: string;
  state: VersionState;
}

export type LogLevel = "info" | "warn" | "error";

export interface LogLine {
  ts: string;
  level: LogLevel;
  msg: string;
  fields: Record<string, string | number>;
}
