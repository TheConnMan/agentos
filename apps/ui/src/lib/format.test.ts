import { describe, expect, it } from "vitest";
import { formatLatency } from "./format";

describe("formatLatency", () => {
  it("renders sub-second latency in milliseconds", () => {
    expect(formatLatency(17.9)).toBe("18ms");
    expect(formatLatency(999)).toBe("999ms");
    expect(formatLatency(0)).toBe("0ms");
  });

  it("renders one-second-and-up latency in seconds with two decimals", () => {
    expect(formatLatency(1000)).toBe("1.00s");
    expect(formatLatency(2100)).toBe("2.10s");
    // The live repro: a ~6.3s p95 came back as 6292 (ms) and was printed as
    // "6292.00s". It must read as seconds, not the raw millisecond count.
    expect(formatLatency(6292)).toBe("6.29s");
  });

  it("returns a neutral dash for non-finite or negative input", () => {
    expect(formatLatency(NaN)).toBe("—");
    expect(formatLatency(-5)).toBe("—");
  });
});
