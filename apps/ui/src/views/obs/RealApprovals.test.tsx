import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StoreProvider } from "../../state/store";
import { RealApprovals } from "./RealApprovals";
import {
  ApiError,
  getApprovalAudit,
  listApprovals,
  resolveApproval,
  type ApprovalAudit,
  type ApprovalOut,
} from "../../api/client";

// Mock only the approvals data-layer calls; keep everything else (ApiError, the
// store, primitives) real.
vi.mock("../../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/client")>();
  return {
    ...actual,
    listApprovals: vi.fn(),
    getApprovalAudit: vi.fn(),
    resolveApproval: vi.fn(),
  };
});

function approval(overrides: Partial<ApprovalOut> = {}): ApprovalOut {
  return {
    id: "ap-1",
    agent_id: "ag-1",
    conversation_id: "C-thread-1",
    author: "U-alice",
    summary: "Refund $4,200 to ACME Corp",
    reply_channel: "C0DEALS",
    reply_placeholder: "ts-1",
    reply_endpoint: null,
    dedupe_key: "dk-1",
    route: "managers",
    card_channel: "C0MANAGERS",
    gate_kind: "permission",
    granted_tool: "issue_refund",
    status: "pending",
    expires_at: "2026-07-24T00:00:00+00:00",
    resolved_by: null,
    resolution_note: null,
    created_at: "2026-07-23T00:00:00+00:00",
    resolved_at: null,
    ...overrides,
  };
}

function renderView() {
  return render(
    <StoreProvider>
      <RealApprovals />
    </StoreProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getApprovalAudit).mockResolvedValue([]);
  try {
    window.localStorage.clear();
  } catch {
    // ignore
  }
});

describe("RealApprovals (#867)", () => {
  it("lists pending approvals by default and requests the pending status", async () => {
    vi.mocked(listApprovals).mockResolvedValue([approval()]);
    renderView();

    expect(await screen.findByText("Refund $4,200 to ACME Corp")).toBeInTheDocument();
    expect(screen.getByText("U-alice")).toBeInTheDocument();
    expect(listApprovals).toHaveBeenCalledWith({ status: "pending" });
  });

  it("shows the pending empty state when nothing is waiting", async () => {
    vi.mocked(listApprovals).mockResolvedValue([]);
    renderView();
    expect(await screen.findByText(/No pending approvals/i)).toBeInTheDocument();
  });

  it("refetches with the chosen status filter (all sends no status)", async () => {
    vi.mocked(listApprovals).mockResolvedValue([]);
    renderView();
    await screen.findByText(/No pending approvals/i);

    await userEvent.selectOptions(screen.getByTestId("approvals-status-filter"), "all");
    await waitFor(() => expect(listApprovals).toHaveBeenLastCalledWith({ status: undefined }));
  });

  it("surfaces a load error", async () => {
    vi.mocked(listApprovals).mockRejectedValue(new Error("boom"));
    renderView();
    expect(await screen.findByTestId("approvals-error")).toHaveTextContent("boom");
  });

  it("opens a detail modal with the audit trail on row click", async () => {
    vi.mocked(listApprovals).mockResolvedValue([approval()]);
    const audit: ApprovalAudit[] = [
      {
        id: "au-1",
        approval_id: "ap-1",
        action: "denied",
        actor: "U-alice",
        actor_channel: "C0DEALS",
        decision: "approved",
        authorizer: "self-approval-block",
        authorized: false,
        reason: "author may not self-approve",
        evidence: null,
        created_at: "2026-07-23T01:00:00+00:00",
      },
    ];
    vi.mocked(getApprovalAudit).mockResolvedValue(audit);
    renderView();

    await userEvent.click(await screen.findByText("Refund $4,200 to ACME Corp"));
    const detail = await screen.findByTestId("approval-detail");
    expect(within(detail).getByText("managers")).toBeInTheDocument();
    expect(await within(detail).findByTestId("approval-audit-entry")).toHaveTextContent("author may not self-approve");
  });

  it("resolves an approval as approved and refreshes the list", async () => {
    vi.mocked(listApprovals).mockResolvedValue([approval()]);
    vi.mocked(resolveApproval).mockResolvedValue(approval({ status: "approved", resolved_by: "you@x.com" }));
    renderView();

    await userEvent.click(await screen.findByText("Refund $4,200 to ACME Corp"));
    await screen.findByTestId("approval-detail");
    await userEvent.type(screen.getByLabelText("resolved by"), "you@x.com");
    await userEvent.click(screen.getByTestId("approve-btn"));

    await waitFor(() =>
      expect(resolveApproval).toHaveBeenCalledWith("ap-1", {
        decision: "approved",
        resolved_by: "you@x.com",
        note: undefined,
        actor_channel: undefined,
      }),
    );
    // Refetch fired after resolve (initial load + reload).
    await waitFor(() => expect(listApprovals).toHaveBeenCalledTimes(2));
  });

  it("blocks resolve until an identity is entered", async () => {
    vi.mocked(listApprovals).mockResolvedValue([approval()]);
    renderView();

    await userEvent.click(await screen.findByText("Refund $4,200 to ACME Corp"));
    await screen.findByTestId("approval-detail");
    await userEvent.click(screen.getByTestId("approve-btn"));

    expect(await screen.findByTestId("resolve-error")).toHaveTextContent(/who is resolving/i);
    expect(resolveApproval).not.toHaveBeenCalled();
  });

  it("surfaces a 409 already-resolved conflict distinctly", async () => {
    vi.mocked(listApprovals).mockResolvedValue([approval()]);
    vi.mocked(resolveApproval).mockRejectedValue(new ApiError(409, "already resolved by U-bob (approved)"));
    renderView();

    await userEvent.click(await screen.findByText("Refund $4,200 to ACME Corp"));
    await screen.findByTestId("approval-detail");
    await userEvent.type(screen.getByLabelText("resolved by"), "you@x.com");
    await userEvent.click(screen.getByTestId("reject-btn"));

    expect(await screen.findByTestId("resolve-error")).toHaveTextContent("Already resolved: already resolved by U-bob");
  });

  it("hides the resolve controls for an already-resolved approval", async () => {
    vi.mocked(listApprovals).mockResolvedValue([
      approval({ status: "approved", resolved_by: "U-bob", resolution_note: "ok" }),
    ]);
    renderView();

    await userEvent.click(await screen.findByText("Refund $4,200 to ACME Corp"));
    await screen.findByTestId("approval-detail");
    expect(screen.queryByTestId("approve-btn")).not.toBeInTheDocument();
  });
});
