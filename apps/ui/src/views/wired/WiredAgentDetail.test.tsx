import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect } from "react";
import { WiredAgentDetail } from "./WiredAgentDetail";
import { StoreProvider, useStore } from "../../state/store";
import { WiredProvider } from "../../state/wired";
import {
  getAgents,
  listVersions,
  listDeployments,
  getVersionFiles,
  updateAgent,
  type AgentOut,
  type VersionOut,
  type DeploymentOut,
  type BundleFiles,
} from "../../api/client";

// Force wired mode so <WiredProvider> fetches getAgents and App/Main would route
// to <WiredAgentDetail>. isWired() otherwise reads window.location (empty in jsdom).
vi.mock("../../api/config", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/config")>()),
  isWired: () => true,
}));

// Mock only the data-layer calls; preserve the real ApiError/BundleValidationError
// classes (the hooks branch on `instanceof ApiError`) and the untouched helpers.
// `updateAgent` is mocked so the channel-edit tests can assert the save button
// issues the PATCH call with the right args without hitting the network.
vi.mock("../../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/client")>();
  return {
    ...actual,
    getAgents: vi.fn(),
    listVersions: vi.fn(),
    listDeployments: vi.fn(),
    getVersionFiles: vi.fn(),
    updateAgent: vi.fn(),
  };
});

const AGENT: AgentOut = {
  id: "a1",
  name: "deal-desk",
  slack_channel: "C0123ABCD",
  created_at: "2026-07-01T00:00:00Z",
};

const VERSION: VersionOut = {
  id: "v1",
  agent_id: "a1",
  version_label: "v0.1.0",
  bundle_ref: "r",
  bundle_sha256: "s",
  created_by: "ui",
  created_at: "2026-07-01T00:00:00Z",
};

const DEPLOYMENT: DeploymentOut = {
  id: "d1",
  agent_id: "a1",
  version_id: "v1",
  environment: "prod",
  bot_identity: null,
  commit_sha: null,
  status: "active",
  deployed_at: "2026-07-01T00:00:00Z",
};

// A bundle with a SKILL.md AND the two non-skill files item 4 must surface.
const FILES: BundleFiles = {
  files: [
    { path: "skills/deal-desk/SKILL.md", content: "---\nname: deal-desk\ndescription: d\n---\n# body" },
    { path: ".claude-plugin/plugin.json", content: '{"name":"deal-desk","version":"v0.1.0"}' },
    { path: "evals/cases.json", content: "[]" },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getAgents).mockResolvedValue([AGENT]);
  vi.mocked(listVersions).mockResolvedValue([VERSION]);
  vi.mocked(listDeployments).mockResolvedValue([DEPLOYMENT]);
  vi.mocked(getVersionFiles).mockResolvedValue(FILES);
  vi.mocked(updateAgent).mockResolvedValue({ ...AGENT, slack_channel: "C9999ZZZZ" });
});

// Render the detail surface directly, dispatching openAgentDetail on mount so the
// store points at the mocked agent (the same action the Agents list dispatches).
function renderDetail() {
  function Harness() {
    const { dispatch } = useStore();
    useEffect(() => {
      dispatch({ type: "openAgentDetail", id: AGENT.id });
    }, [dispatch]);
    return <WiredAgentDetail />;
  }
  // WiredAgentDetail consumes react-query hooks (useAgentVersions/useVersionFiles),
  // so it needs a QueryClientProvider. A fresh client per render with retry off,
  // mirroring main.tsx and hooks.rq.test.tsx, so error/404 sentinels resolve on the
  // first response instead of being retried.
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <StoreProvider level={3}>
        <WiredProvider>
          <Harness />
        </WiredProvider>
      </StoreProvider>
    </QueryClientProvider>,
  );
}

describe("WiredAgentDetail — channel edit (item 5)", () => {
  it("saves an edited Slack channel via updateAgent and refetches the agent list", async () => {
    const user = userEvent.setup();
    renderDetail();

    // Agent header loads once getAgents resolves; getAgents fired exactly once.
    expect(await screen.findByTestId("agent-detail-name")).toHaveTextContent("deal-desk");
    await waitFor(() => expect(getAgents).toHaveBeenCalledTimes(1));

    // The channel is now an editable input pre-filled with the current value.
    const input = await screen.findByTestId("channel-input");
    expect(input).toHaveValue("C0123ABCD");

    await user.clear(input);
    await user.type(input, "C9999ZZZZ");
    await user.click(screen.getByTestId("channel-save"));

    // Exactly one PATCH, with the right args.
    await waitFor(() => expect(updateAgent).toHaveBeenCalledTimes(1));
    expect(updateAgent).toHaveBeenCalledWith("a1", { slack_channel: "C9999ZZZZ" });

    // Save triggers a refetch of the wired agent data (getAgents runs a 2nd time).
    await waitFor(() => expect(getAgents).toHaveBeenCalledTimes(2));
  });

  it("blocks saving an empty channel (no updateAgent call)", async () => {
    const user = userEvent.setup();
    renderDetail();

    const input = await screen.findByTestId("channel-input");
    await user.clear(input);

    // Save is disabled / a no-op while the channel is empty.
    const save = screen.getByTestId("channel-save");
    await user.click(save).catch(() => {});
    expect(updateAgent).not.toHaveBeenCalled();
  });

  it("warns on a non-Slack-ID channel but still allows saving (soft check)", async () => {
    const user = userEvent.setup();
    renderDetail();

    const input = await screen.findByTestId("channel-input");
    await user.clear(input);
    await user.type(input, "revenue-ops");

    // Soft warning shows (mirrors NewAgentModal's CHANNEL_ID_RE warning)…
    expect(screen.getByTestId("channel-warn")).toBeInTheDocument();

    // …but the value still saves.
    await user.click(screen.getByTestId("channel-save"));
    await waitFor(() => expect(updateAgent).toHaveBeenCalledWith("a1", { slack_channel: "revenue-ops" }));
  });
});

describe("WiredAgentDetail — bundle tree (item 4)", () => {
  it("renders every bundle file, not just SKILL.md", async () => {
    renderDetail();
    expect(await screen.findByTestId("agent-detail-name")).toBeInTheDocument();

    // The full bundle tree is visible: the non-skill files are findable in the DOM.
    await waitFor(() => {
      expect(screen.getAllByText("evals/cases.json").length).toBeGreaterThan(0);
      expect(screen.getAllByText(".claude-plugin/plugin.json").length).toBeGreaterThan(0);
    });

    // SKILL.md is still present so the existing edit/deploy path is not lost.
    expect(screen.getAllByText("skills/deal-desk/SKILL.md").length).toBeGreaterThan(0);
  });
});
