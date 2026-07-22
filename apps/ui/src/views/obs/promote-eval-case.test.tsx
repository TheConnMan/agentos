import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect } from "react";
import { RealTraceDetail } from "./RealTraces";
import { StoreProvider, useStore } from "../../state/store";
import { getTrace, promoteTraceToEvalCase, type TraceTree, type EvalCaseOut } from "../../api/client";

// Mock only the data-layer calls; keep the real store + component wiring.
vi.mock("../../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/client")>();
  return { ...actual, getTrace: vi.fn(), promoteTraceToEvalCase: vi.fn() };
});

const TRACE: TraceTree = {
  trace: { id: "tr-1", name: "agentos-run:agent-x-thread-1" },
  tree: [{ id: "root", type: "SPAN", name: "agent.run", model: null, startTime: "1", usageDetails: null, children: [] }],
  sandbox_id: null,
};

const CASE: EvalCaseOut = {
  id: "promoted-tr-1",
  input: "What is the refund policy?",
  grader: { kind: "contains", expected: "30 days", case_sensitive: false },
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getTrace).mockResolvedValue(TRACE);
  vi.mocked(promoteTraceToEvalCase).mockResolvedValue(CASE);
});

function renderWired(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <StoreProvider>{ui}</StoreProvider>
    </QueryClientProvider>,
  );
}

// Surfaces the promoted-eval-case count + latest toast so the test can assert the
// case landed in the store.
function StoreProbe() {
  const { state } = useStore();
  return (
    <div data-testid="store-probe">{`${state.promotedEvalCases.length}:${state.promotedEvalCases[0]?.id ?? ""}:${state.toast ?? ""}`}</div>
  );
}

function Harness() {
  const { dispatch } = useStore();
  useEffect(() => {
    dispatch({ type: "openTrace", id: "tr-1" });
  }, [dispatch]);
  return (
    <>
      <RealTraceDetail />
      <StoreProbe />
    </>
  );
}

describe("RealTraceDetail — promote to eval case (#259)", () => {
  it("promotes the open trace and stores the anonymized case", async () => {
    const user = userEvent.setup();
    renderWired(<Harness />);

    const btn = await screen.findByTestId("promote-eval-case");
    await user.click(btn);

    await waitFor(() => expect(promoteTraceToEvalCase).toHaveBeenCalledWith("tr-1"));
    // The returned case lands in the store (newest first) and a toast fires.
    await waitFor(() =>
      expect(screen.getByTestId("store-probe")).toHaveTextContent("1:promoted-tr-1:"),
    );
    expect(screen.getByTestId("store-probe")).toHaveTextContent("Promoted to eval case promoted-tr-1");
  });

  it("surfaces an error and does not store a case when promotion fails", async () => {
    vi.mocked(promoteTraceToEvalCase).mockRejectedValue(new Error("boom"));
    const user = userEvent.setup();
    renderWired(<Harness />);

    const btn = await screen.findByTestId("promote-eval-case");
    await user.click(btn);

    expect(await screen.findByTestId("promote-error")).toHaveTextContent("boom");
    expect(screen.getByTestId("store-probe")).toHaveTextContent("0::");
  });
});
