import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CliHint } from "./CliHint";
import { StoreProvider, useStore } from "../state/store";

// Surfaces the store's toast so a copy can be asserted without styling internals.
function ToastProbe() {
  const { state } = useStore();
  return <span data-testid="toast">{state.toast ?? ""}</span>;
}

function renderHint(props: { command: string; label?: string }) {
  return render(
    <StoreProvider>
      <CliHint {...props} />
      <ToastProbe />
    </StoreProvider>,
  );
}

const writeText = vi.fn(() => Promise.resolve());

function setClipboard(value: unknown) {
  Object.defineProperty(navigator, "clipboard", {
    value,
    configurable: true,
    writable: true,
  });
}

beforeEach(() => {
  writeText.mockClear();
  setClipboard({ writeText });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("CliHint", () => {
  it("renders the resting >_ glyph", () => {
    renderHint({ command: "curie skill up" });
    expect(screen.getByRole("button")).toHaveTextContent(">_");
  });

  it("previews the exact command in a tooltip and aria-label", () => {
    renderHint({ command: "curie skill up" });
    const btn = screen.getByRole("button");
    expect(btn).toHaveAttribute("title", "$ curie skill up");
    expect(btn).toHaveAccessibleName("Copy command: curie skill up");
  });

  it("morphs to the copy glyph on hover and back on leave", async () => {
    const user = userEvent.setup();
    renderHint({ command: "curie skill up" });
    const btn = screen.getByRole("button");
    await user.hover(btn);
    expect(btn).toHaveTextContent("⧉");
    await user.unhover(btn);
    expect(btn).toHaveTextContent(">_");
  });

  it("morphs on keyboard focus and back on blur", async () => {
    const user = userEvent.setup();
    renderHint({ command: "curie skill up" });
    const btn = screen.getByRole("button");
    await user.tab();
    expect(btn).toHaveFocus();
    expect(btn).toHaveTextContent("⧉");
    await user.tab();
    expect(btn).toHaveTextContent(">_");
  });

  it("copies on click: writes the command, toasts, and flips to ✓", async () => {
    const user = userEvent.setup();
    setClipboard({ writeText }); // after setup(), which installs its own stub
    renderHint({ command: "curie skill up" });
    const btn = screen.getByRole("button");
    await user.click(btn);
    expect(writeText).toHaveBeenCalledWith("curie skill up");
    expect(screen.getByTestId("toast")).toHaveTextContent("Copied");
    expect(btn).toHaveAttribute("data-copied", "true");
    expect(btn).toHaveTextContent("✓");
    // The ✓ resets after the timeout.
    await waitFor(() => expect(btn).toHaveAttribute("data-copied", "false"), {
      timeout: 2500,
    });
  });

  it("copies via keyboard (Enter) for accessibility", async () => {
    const user = userEvent.setup();
    setClipboard({ writeText }); // after setup(), which installs its own stub
    renderHint({ command: "curie skill up" });
    const btn = screen.getByRole("button");
    btn.focus();
    await user.keyboard("{Enter}");
    expect(writeText).toHaveBeenCalledWith("curie skill up");
  });

  it("renders an optional label", () => {
    renderHint({ command: "curie skill up", label: "Run it" });
    expect(screen.getByRole("button")).toHaveTextContent("Run it");
  });

  it("still toasts when the clipboard API is unavailable", async () => {
    const user = userEvent.setup();
    setClipboard(undefined); // after setup(), which installs its own stub
    renderHint({ command: "curie skill up" });
    await user.click(screen.getByRole("button"));
    expect(writeText).not.toHaveBeenCalled();
    expect(screen.getByTestId("toast")).toHaveTextContent("Copied");
  });
});
