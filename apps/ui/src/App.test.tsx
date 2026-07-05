import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { StoreProvider } from "./state/store";
import { WiredProvider } from "./state/wired";
import type { FixtureLevel } from "./state/types";

// These tests exercise the fixture shell (no ?api=1), so WiredProvider is inert
// here — it just satisfies the useWired() contract the shared chrome now uses.
function renderAt(level: FixtureLevel) {
  return render(
    <StoreProvider level={level}>
      <WiredProvider>
        <App />
      </WiredProvider>
    </StoreProvider>,
  );
}

describe("App shell", () => {
  it("renders the seven nav items", () => {
    renderAt(6);
    const nav = screen.getByRole("navigation");
    for (const label of ["Overview", "Agents", "Evals", "Observability", "Versions", "Connections", "Settings"]) {
      expect(within(nav).getByText(label)).toBeInTheDocument();
    }
  });

  it("shows the setup checklist for a fresh account", () => {
    renderAt(1);
    expect(screen.getByText("Welcome to AgentOS")).toBeInTheDocument();
    expect(screen.getByText("Connect Slack")).toBeInTheDocument();
  });

  it("renders the fleet dashboard at level 6", () => {
    renderAt(6);
    expect(screen.getByText("acme-corp fleet · 5 agents")).toBeInTheDocument();
    expect(screen.getByText("rev-analytics")).toBeInTheDocument();
  });

  it("navigates to Evals and shows the matrix regression story", async () => {
    const user = userEvent.setup();
    renderAt(4);
    await user.click(within(screen.getByRole("navigation")).getByText("Evals"));
    await user.click(screen.getByText("Matrix"));
    await user.click(screen.getByRole("button", { name: "Run matrix" }));
    expect(screen.getByText(/2 regressions introduced after 4f2c91a/)).toBeInTheDocument();
  });

  it("completes the create-agent deploy flow into the success panel", async () => {
    const user = userEvent.setup();
    renderAt(2);
    await user.click(within(screen.getByRole("navigation")).getByText("Agents"));
    await user.click(screen.getByRole("button", { name: /New agent/ }));
    await user.click(screen.getByRole("button", { name: "Deploy" }));
    // deploy resolves after 700ms; assert the success banner appears
    expect(await screen.findByText(/is live in #revenue-ops/, undefined, { timeout: 2000 })).toBeInTheDocument();
  });
});
