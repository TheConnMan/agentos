import { afterEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { PropsWithChildren } from "react";
import { createElement } from "react";
import { useTrace, useVersionFiles } from "./hooks";

afterEach(() => {
  vi.unstubAllGlobals();
});

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

// A fresh client per test with retry off, mirroring main.tsx, so error/404
// sentinels resolve on the first response instead of being retried.
function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: PropsWithChildren) =>
    createElement(QueryClientProvider, { client }, children);
}

describe("useTrace (react-query)", () => {
  it("maps a 404 to notFound=true with error===null (not an error)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(404, { detail: "trace has no observations yet" })),
    );
    const { result } = renderHook(() => useTrace("t1"), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.notFound).toBe(true);
    expect(result.current.error).toBeNull();
    expect(result.current.data).toBeNull();
  });

  it("returns the trace tree on a 200", async () => {
    const tree = { trace: { id: "t1" }, tree: [] };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(200, tree)));
    const { result } = renderHook(() => useTrace("t1"), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.notFound).toBe(false);
    expect(result.current.error).toBeNull();
    expect(result.current.data).toEqual(tree);
  });
});

describe("useVersionFiles (react-query)", () => {
  it("maps a 404 to noBundle=true with error===null (not an error)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(404, { detail: "no bundle stored for this version" })),
    );
    const { result } = renderHook(() => useVersionFiles("a1", "v9"), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.noBundle).toBe(true);
    expect(result.current.error).toBeNull();
    expect(result.current.files).toBeNull();
  });

  it("returns the bundle files on a 200", async () => {
    const files = [{ path: "skills/x/SKILL.md", content: "body" }];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(200, { files })));
    const { result } = renderHook(() => useVersionFiles("a1", "v1"), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.noBundle).toBe(false);
    expect(result.current.error).toBeNull();
    expect(result.current.files).toEqual(files);
  });
});
