import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { cliCommand } from "./cliCommand";
import { CliHint } from "./CliHint";
import { StoreProvider } from "../state/store";
import { commandManifest } from "../generated/commandManifest";

// cliCommand is typed against the CLI manifest; these assert the *runtime*
// rendering (the type teeth are exercised by `pnpm typecheck`, not vitest).
describe("cliCommand", () => {
  it("renders a bare leaf command with no context", () => {
    expect(cliCommand("skill.up")).toBe("agentos skill up");
  });

  it("renders a nested-group leaf path", () => {
    expect(cliCommand("dev.contracts")).toBe("agentos dev contracts");
  });

  it("places a positional arg before flags, in clap order", () => {
    // `text` is positional; `channel`/`user` are flags -> positional leads.
    const cmd = cliCommand("local.message", {
      channel: "C123",
      text: "ship it",
      user: "U9",
    });
    expect(cmd).toBe('agentos local message "ship it" --channel C123 --user U9');
  });

  it("renders boolean flags as a bare --flag and omits false ones", () => {
    expect(cliCommand("local.up", { "dry-run": true, slack: false })).toBe(
      "agentos local up --dry-run",
    );
  });

  it("quotes only values containing whitespace", () => {
    expect(cliCommand("skill.message", { text: "one", user: "U1" })).toBe(
      "agentos skill message one --user U1",
    );
    expect(cliCommand("skill.message", { text: "two words" })).toBe(
      'agentos skill message "two words"',
    );
  });

  it("throws on a context key that is not an arg of the command", () => {
    // Cast through unknown to reach the runtime guard (the typed path forbids it).
    const bad = cliCommand as unknown as (a: string, c: Record<string, unknown>) => string;
    expect(() => bad("local.message", { nope: "x" })).toThrow(/not an argument/);
  });

  it("throws on an unknown command id", () => {
    const bad = cliCommand as unknown as (a: string) => string;
    expect(() => bad("local.nope")).toThrow(/unknown command/);
  });

  it("only resolves ids that exist in the imported manifest", () => {
    // Sanity: the manifest is the source of truth the resolver walks.
    const names = commandManifest.subcommands.map((s) => s.name);
    expect(names).toContain("local");
    expect(names).toContain("skill");
  });

  it("feeds CliHint a resolved command that is copied verbatim", () => {
    const command = cliCommand("local.message", { text: "hi", channel: "C1" });
    render(
      <StoreProvider>
        <CliHint command={command} label="run it" />
      </StoreProvider>,
    );
    const btn = screen.getByRole("button", { name: `Copy command: ${command}` });
    expect(btn).toHaveAttribute("title", `$ ${command}`);
  });
});
