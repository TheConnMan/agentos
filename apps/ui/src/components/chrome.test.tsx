import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { Topbar } from "./Topbar";
import { Sidebar } from "./Sidebar";
import { StoreProvider } from "../state/store";
import { isWired } from "../api/config";
import { useWired, type WiredData } from "../state/wired";

// The shared chrome (Topbar + Sidebar) shows the workspace name. In wired mode
// it comes from the real config the wired data layer exposes as `orgName`; in
// fixture/demo mode it stays the hardcoded `acme-corp`. We mock the mode gate
// (isWired) and the wired data layer (useWired) so we can render just the chrome
// deterministically, without a live fetch — the same wired-vs-fixture split the
// App.test.tsx harness relies on, isolated to the two chrome components.
vi.mock("../api/config", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/config")>();
  return { ...actual, isWired: vi.fn() };
});
vi.mock("../state/wired", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../state/wired")>();
  return { ...actual, useWired: vi.fn() };
});

// A full WiredData, plus the new `orgName` field, cast so this test compiles
// before the field lands on the interface.
function wiredValue(over: Record<string, unknown>): WiredData {
  return {
    wired: false,
    agents: [],
    loading: false,
    error: null,
    refetch: () => {},
    justDeployed: null,
    markDeployed: () => {},
    clearDeployed: () => {},
    ...over,
  } as unknown as WiredData;
}

function renderChrome() {
  return render(
    <StoreProvider level={6}>
      <Topbar />
      <Sidebar />
    </StoreProvider>,
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("chrome workspace name", () => {
  it("renders the configured org name in wired mode (Topbar + Sidebar)", () => {
    vi.mocked(isWired).mockReturnValue(true);
    vi.mocked(useWired).mockReturnValue(wiredValue({ wired: true, orgName: "Globex Corporation" }));
    renderChrome();
    // Both the Topbar breadcrumb and the Sidebar workspace button show it.
    expect(screen.getAllByText("Globex Corporation").length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText("acme-corp")).toBeNull();
  });

  it("keeps the hardcoded acme-corp in fixture/demo mode", () => {
    vi.mocked(isWired).mockReturnValue(false);
    vi.mocked(useWired).mockReturnValue(wiredValue({ wired: false, orgName: "AgentOS" }));
    renderChrome();
    expect(screen.getAllByText("acme-corp").length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText("Globex Corporation")).toBeNull();
  });
});
