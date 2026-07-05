import { describe, expect, it } from "vitest";
import { agentsForLevel, ALL_AGENTS } from "./agents";
import { tracesForLevel, traceSpans } from "./traces";
import { EVAL_CASES, MATRIX_VERSIONS } from "./evals";
import { logsForLevel } from "./logs";

describe("agent fixtures", () => {
  it("reveals agents as the fixture level climbs", () => {
    expect(agentsForLevel(1)).toHaveLength(0);
    expect(agentsForLevel(3)).toHaveLength(1);
    expect(agentsForLevel(5)).toHaveLength(2);
    expect(agentsForLevel(6)).toHaveLength(5);
  });

  it("seeds rev-analytics as the degrading agent", () => {
    const rev = ALL_AGENTS.find((a) => a.id === "rev-analytics");
    expect(rev?.health).toBe("amber");
    // trend ends below where it started
    expect(rev!.trend[rev!.trend.length - 1]).toBeLessThan(rev!.trend[0]);
  });
});

describe("trace fixtures", () => {
  it("only surfaces the failed trace once CI is on (level 4+)", () => {
    expect(tracesForLevel(3).every((t) => t.status === "ok")).toBe(true);
    expect(tracesForLevel(4).some((t) => t.status === "fail")).toBe(true);
  });

  it("builds the span waterfall from ACI-typed events", () => {
    const fail = tracesForLevel(4).find((t) => t.status === "fail")!;
    const spans = traceSpans(fail);
    expect(spans).toHaveLength(4);
    // the inbound message is an ACI Event, the terminal span an ACI ErrorEvent
    expect(spans[0].event.type).toBe("message");
    const last = spans[spans.length - 1];
    expect(last.bad).toBe(true);
    expect(last.event.type).toBe("error");
  });
});

describe("eval fixtures", () => {
  it("encodes a real regression: passes on v1.4.2, fails on the dev sonnet builds", () => {
    const regression = EVAL_CASES.find((c) => c.n === "deal-data-from-crm-not-slack")!;
    expect(regression.s[0]).toBe(1); // v1.4.2
    expect(regression.s[1]).toBe(0); // 4f2c91a sonnet
    expect(regression.s[2]).toBe(0); // b7e02d1 sonnet
  });

  it("has four version columns matching the case width", () => {
    expect(MATRIX_VERSIONS).toHaveLength(4);
    expect(EVAL_CASES.every((c) => c.s.length === 4)).toBe(true);
  });
});

describe("log fixtures", () => {
  it("gates the error line behind CI", () => {
    expect(logsForLevel(false).some((l) => l.level === "error")).toBe(false);
    expect(logsForLevel(true).some((l) => l.level === "error")).toBe(true);
  });
});
