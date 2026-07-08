import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useEffect } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { WiredAgentDetail } from "./WiredAgentDetail";
import { StoreProvider, useStore } from "../../state/store";
import { WiredProvider } from "../../state/wired";
import {
  getAgents,
  listVersions,
  listDeployments,
  getVersionFiles,
  createDeployment,
} from "../../api/client";
import type { AgentOut, VersionOut, DeploymentOut } from "../../api/client";

// Wire the account on so WiredProvider fetches agents and the detail surface renders.
vi.mock("../../api/config", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/config")>();
  return { ...actual, isWired: vi.fn(() => true) };
});

// Mock only the network functions; keep ApiError / BundleValidationError real so
// the component's instanceof checks behave.
vi.mock("../../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/client")>();
  return {
    ...actual,
    getAgents: vi.fn(),
    listVersions: vi.fn(),
    listDeployments: vi.fn(),
    getVersionFiles: vi.fn(),
    createVersion: vi.fn(),
    uploadBundle: vi.fn(),
    createDeployment: vi.fn(),
  };
});

const AGENT: AgentOut = { id: "a1", name: "deal-desk", slack_channel: "#revenue-ops", created_at: "2026-07-01T00:00:00Z" };

const version = (id: string, label: string): VersionOut => ({
  id,
  agent_id: "a1",
  version_label: label,
  bundle_ref: "ref",
  bundle_sha256: "sha",
  created_by: "ui",
  created_at: "2026-07-01T00:00:00Z",
});

const deployment = (version_id: string, environment: "prod" | "dev", deployed_at: string): DeploymentOut => ({
  id: `dep-${version_id}-${environment}`,
  agent_id: "a1",
  version_id,
  environment,
  bot_identity: null,
  commit_sha: null,
  status: "active",
  deployed_at,
});

// v1 is the prod-active version (what the editor loads); v2 is the dev-active
// version — the one promote-to-prod must ship.
const VERSIONS = [version("v1", "v0.1.0"), version("v2", "v0.2.0")];
const DEPLOYMENTS = [
  deployment("v1", "prod", "2026-07-05T00:00:00Z"),
  deployment("v2", "dev", "2026-07-07T00:00:00Z"),
];
const FILES = {
  files: [{ path: "skills/deal-desk/SKILL.md", content: "---\nname: deal-desk\ndescription: d\n---\nbody" }],
};

function Harness() {
  const { state, dispatch } = useStore();
  useEffect(() => {
    dispatch({ type: "openAgentDetail", id: "a1" });
  }, [dispatch]);
  return state.agentDetail === "a1" ? <WiredAgentDetail /> : null;
}

function renderDetail() {
  return render(
    <StoreProvider level={1}>
      <WiredProvider>
        <Harness />
      </WiredProvider>
    </StoreProvider>,
  );
}

beforeEach(() => {
  vi.mocked(getAgents).mockResolvedValue([AGENT]);
  vi.mocked(listVersions).mockResolvedValue(VERSIONS);
  vi.mocked(listDeployments).mockResolvedValue(DEPLOYMENTS);
  vi.mocked(getVersionFiles).mockResolvedValue(FILES);
  vi.mocked(createDeployment).mockResolvedValue(deployment("v2", "prod", "2026-07-08T00:00:00Z"));
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("WiredAgentDetail promote-to-prod", () => {
  it("promotes the dev-active version to prod on confirm, then refreshes", async () => {
    const user = userEvent.setup();
    renderDetail();

    // The detail body renders once agents + versions + files have loaded.
    expect(await screen.findByTestId("agent-detail-name")).toHaveTextContent("deal-desk");

    const promote = await screen.findByRole("button", { name: "Promote to prod" });
    expect(vi.mocked(listDeployments)).toHaveBeenCalledTimes(1);

    await user.click(promote);
    // Inline confirm (kill-switch pattern): nothing deployed until confirmed.
    await user.click(await screen.findByRole("button", { name: "Confirm promote" }));

    await waitFor(() => expect(vi.mocked(createDeployment)).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createDeployment)).toHaveBeenCalledWith({
      agent_id: "a1",
      version_id: "v2",
      environment: "prod",
    });

    // A refresh follows the promote (re-fetch versions + deployments).
    await waitFor(() => expect(vi.mocked(listDeployments).mock.calls.length).toBeGreaterThan(1));
  });

  it("deploys nothing when the promote confirm is cancelled", async () => {
    const user = userEvent.setup();
    renderDetail();

    const promote = await screen.findByRole("button", { name: "Promote to prod" });
    await user.click(promote);
    await user.click(await screen.findByRole("button", { name: "Cancel" }));

    expect(vi.mocked(createDeployment)).not.toHaveBeenCalled();
  });
});
