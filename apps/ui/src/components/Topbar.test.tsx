import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Topbar } from "./Topbar";
import { StoreProvider, useStore } from "../state/store";
import { WiredProvider } from "../state/wired";
import { getAgents, getConfig } from "../api/client";

// The console is always backed by the live API, so the env switcher is always
// enabled. We mock the data layer so WiredProvider resolves without a fetch.
vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, getAgents: vi.fn(), getConfig: vi.fn() };
});

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getAgents).mockResolvedValue([]);
  vi.mocked(getConfig).mockResolvedValue({ org_name: "Curie" });
});

// Surfaces the store's env state so assertions do not depend on pill styling.
function Probe() {
  const { state } = useStore();
  return <span data-testid="env">{state.env}</span>;
}

function renderTopbar() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <StoreProvider>
        <WiredProvider>
          <Topbar />
          <Probe />
        </WiredProvider>
      </StoreProvider>
    </QueryClientProvider>,
  );
}

describe("Topbar env switcher", () => {
  it("starts on prod and toggles between DEV and PROD", async () => {
    const user = userEvent.setup();
    renderTopbar();

    expect(screen.getByTestId("env")).toHaveTextContent("prod");

    await user.click(screen.getByRole("button", { name: "DEV" }));
    expect(screen.getByTestId("env")).toHaveTextContent("dev");

    await user.click(screen.getByRole("button", { name: "PROD" }));
    expect(screen.getByTestId("env")).toHaveTextContent("prod");
  });
});
