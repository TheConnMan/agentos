import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Topbar } from "./Topbar";
import { StoreProvider, useStore } from "../state/store";
import { WiredProvider } from "../state/wired";
import { isWired } from "../api/config";
import type { FixtureLevel } from "../state/types";

// The env switcher's enablement is derived in the store. After the fix it is
// driven by isWired() OR a level>=4 fixture, so we mock the wiring flag and
// assert the pill behaviour through the store.
vi.mock("../api/config", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/config")>();
  return { ...actual, isWired: vi.fn(() => false) };
});

afterEach(() => {
  vi.mocked(isWired).mockReset();
  vi.mocked(isWired).mockReturnValue(false);
});

// Surfaces the store's derived env-switch state so the assertions do not depend
// on pill styling internals.
function Probe() {
  const { state, ghOn } = useStore();
  return (
    <>
      <span data-testid="env">{state.env}</span>
      <span data-testid="ghon">{String(ghOn)}</span>
    </>
  );
}

function renderTopbar(level: FixtureLevel) {
  // Topbar now reads useWired() (org name) and the tree mounts react-query
  // hooks, so wrap in the same provider stack main.tsx uses. retry off keeps
  // any incidental query deterministic.
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <StoreProvider level={level}>
        <WiredProvider>
          <Topbar />
          <Probe />
        </WiredProvider>
      </StoreProvider>
    </QueryClientProvider>,
  );
}

describe("Topbar env switcher enablement", () => {
  it("is enabled in wired mode even at a low fixture level, and DEV dispatches setEnv('dev')", async () => {
    vi.mocked(isWired).mockReturnValue(true);
    const user = userEvent.setup();
    renderTopbar(1);

    // Enabled: the store reports environments are available.
    expect(screen.getByTestId("ghon")).toHaveTextContent("true");
    expect(screen.getByTestId("env")).toHaveTextContent("prod");

    await user.click(screen.getByRole("button", { name: "DEV" }));
    expect(screen.getByTestId("env")).toHaveTextContent("dev");

    await user.click(screen.getByRole("button", { name: "PROD" }));
    expect(screen.getByTestId("env")).toHaveTextContent("prod");
  });

  it("stays disabled in fixture mode below level 4 — clicking DEV is a no-op", async () => {
    vi.mocked(isWired).mockReturnValue(false);
    const user = userEvent.setup();
    renderTopbar(1);

    expect(screen.getByTestId("ghon")).toHaveTextContent("false");

    await user.click(screen.getByRole("button", { name: "DEV" }));
    // Disabled pill: env is unchanged.
    expect(screen.getByTestId("env")).toHaveTextContent("prod");
  });
});
