import { describe, expect, it } from "vitest";
import { sandboxIdFromTrace } from "./RealTraces";

describe("sandboxIdFromTrace", () => {
  it("reads agentos.sandbox_id from trace metadata", () => {
    expect(sandboxIdFromTrace({ metadata: { "agentos.sandbox_id": "runner-deal-desk-abc123" } })).toBe(
      "runner-deal-desk-abc123",
    );
  });

  it("reads it from OTel resource attributes", () => {
    expect(
      sandboxIdFromTrace({ resourceAttributes: { attributes: { "agentos.sandbox_id": "sbx-9" } } }),
    ).toBe("sbx-9");
  });

  it("accepts the bare sandbox_id key too", () => {
    expect(sandboxIdFromTrace({ metadata: { sandbox_id: "sbx-bare" } })).toBe("sbx-bare");
  });

  it("returns null when absent or blank, so the UI degrades silently", () => {
    expect(sandboxIdFromTrace({ metadata: { other: "x" } })).toBeNull();
    expect(sandboxIdFromTrace({ metadata: { "agentos.sandbox_id": "" } })).toBeNull();
    expect(sandboxIdFromTrace(null)).toBeNull();
    expect(sandboxIdFromTrace(undefined)).toBeNull();
  });
});
