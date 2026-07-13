import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { CliHint } from "./CliHint";
import { cliCommand } from "./cliCommand";
import { WIRED_ACTIONS, PARITY_TRACKING_ISSUE } from "./parity";
import { StoreProvider } from "../state/store";

// Console/CLI parity gate (epic #145): every wired UI action must map to EITHER
// a real command resolved from the committed manifest OR an explicit
// `noCliEquivalent` marker pointing at the tracking issue. This test enumerates
// the parity registry and fails the moment a wired action ships without a
// deliberate mapping — the honest amber state is only for genuinely-unmapped
// actions, never a silent gap.

describe("console/CLI parity registry (#280)", () => {
  it("lists at least the known wired actions", () => {
    // A guard against the registry being emptied/short-circuited.
    expect(WIRED_ACTIONS.length).toBeGreaterThanOrEqual(10);
  });

  it("every wired action maps to a real command or an explicit noCliEquivalent", () => {
    for (const action of WIRED_ACTIONS) {
      if ("command" in action.mapping) {
        // Resolves against the manifest without throwing, and yields an
        // `agentos …` invocation. `cliCommand` throws on an unknown path, so
        // this catches a command id that is not a real manifest leaf.
        const rendered = cliCommand(action.mapping.command);
        expect(rendered.startsWith("agentos ")).toBe(true);
      } else {
        // The honest gap: a tracking-issue URL, not an empty string.
        expect(action.mapping.noCliEquivalent).toMatch(/^https?:\/\//);
      }
    }
  });

  it("the four former parity-gap actions now resolve to real CLI verbs", () => {
    // #149 landed kill/resume/budget/delete, so these are commands, not gaps.
    for (const id of ["kill", "resume", "budget", "delete"]) {
      const action = WIRED_ACTIONS.find((a) => a.id === id);
      expect(action, `missing wired action: ${id}`).toBeDefined();
      expect("command" in action!.mapping).toBe(true);
    }
  });
});

describe("CliHint — noCliEquivalent amber state (#280)", () => {
  function renderHint(el: React.ReactElement) {
    return render(<StoreProvider level={3}>{el}</StoreProvider>);
  }

  it("renders the amber glyph and links to the tracking issue instead of copying", () => {
    renderHint(<CliHint noCliEquivalent={PARITY_TRACKING_ISSUE} />);
    const btn = screen.getByRole("button", { name: /no cli equivalent/i });
    expect(btn).toHaveAttribute("data-no-cli", "true");
    // It carries no "Copy command" affordance.
    expect(screen.queryByRole("button", { name: /copy command/i })).toBeNull();
  });

  it("still renders the copy affordance for a real command", () => {
    renderHint(<CliHint command="agentos cluster deploy" />);
    expect(
      screen.getByRole("button", { name: "Copy command: agentos cluster deploy" }),
    ).toBeInTheDocument();
  });
});
