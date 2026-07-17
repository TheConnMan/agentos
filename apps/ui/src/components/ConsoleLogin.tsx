import { useState } from "react";
import { C, R } from "../tokens";
import { Button } from "../primitives";
import { cliCommand } from "../primitives/cliCommand";
import { ApiError } from "../api/client";

// The console's login gate (#630 / ADR-0049). The operator pastes a single-use
// login code minted by the AgentOS CLI; the exchange sets an HttpOnly session
// cookie server-side. Nothing here is a password: the code is spent on the
// first exchange, it is never stored, and this component holds it only as
// component state for as long as the field shows it.
//
// Ported from the existing design language: the card/border elevation and mono
// input treatment already used by the create-agent modal, no new visual idiom.

export function ConsoleLogin({ onActivate }: { onActivate: (code: string) => Promise<void> }) {
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit() {
    const value = code.trim();
    if (!value || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onActivate(value);
      // On success the gate swaps this view out, so there is no state to reset.
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not reach the API.");
      setBusy(false);
    }
  }

  return (
    <div
      data-testid="console-login"
      style={{
        fontFamily: C.sans,
        color: C.text,
        background: C.page,
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 24,
      }}
    >
      <div style={{ width: "100%", maxWidth: 420 }}>
        <div
          style={{
            background: C.card,
            border: "1px solid " + C.border,
            borderRadius: R.cardLg,
            padding: 28,
          }}
        >
          <h1 style={{ fontSize: 19, fontWeight: 400, color: C.text, margin: "0 0 6px" }}>Sign in to AgentOS</h1>
          <p style={{ fontSize: 13, color: C.muted, lineHeight: 1.5, margin: "0 0 12px" }}>
            Mint a login code with the AgentOS CLI on this install, then paste it below. Codes are single use and
            expire.
          </p>
          {/*
            Both tiers are shown because this view CANNOT know which one it is
            serving: `env` is a store concept, and the gate mounts above
            StoreProvider on purpose (a locked console must not mount the
            providers that fetch). Naming one tier would be a guess that is wrong
            half the time, so the operator picks the line matching their install.

            Plain text rather than a <CliHint>: CliHint calls useStore() for its
            "Copied" toast, and there is no store above the gate. The parity rule
            in apps/ui/CLAUDE.md is that the command STRING resolves from the
            committed manifest via cliCommand() -- which it does here -- not that
            it must be carried by CliHint.
          */}
          <div
            data-testid="console-login-cli"
            style={{
              background: C.input,
              border: "1px solid " + C.border,
              borderRadius: R.btn,
              padding: "8px 10px",
              fontFamily: C.mono,
              fontSize: 12,
              color: C.muted,
              lineHeight: 1.7,
              marginBottom: 20,
            }}
          >
            <div>{cliCommand("cluster.console.login")}</div>
            <div>{cliCommand("local.console.login")}</div>
          </div>

          <label htmlFor="console-login-code" style={{ fontSize: 13, fontWeight: 500, display: "block", marginBottom: 6 }}>
            Login code
          </label>
          <input
            id="console-login-code"
            data-testid="console-login-code"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void submit();
            }}
            autoFocus
            autoComplete="off"
            spellCheck={false}
            placeholder="AAAA-BBBB-CCCC"
            style={{
              width: "100%",
              background: C.input,
              border: "1px solid " + (error ? C.destructive : C.borderStrong),
              borderRadius: R.input,
              padding: "8px 10px",
              color: C.text,
              fontFamily: C.mono,
              fontSize: 13,
              marginBottom: 14,
            }}
          />

          {error ? (
            <div
              data-testid="console-login-error"
              style={{
                border: "1px solid rgba(229,77,46,.3)",
                background: "rgba(229,77,46,.06)",
                borderRadius: R.btn,
                padding: "8px 10px",
                fontSize: 12.5,
                color: C.destructive,
                fontFamily: C.mono,
                marginBottom: 14,
              }}
            >
              {error}
            </div>
          ) : null}

          <Button
            label={busy ? "Signing in…" : "Sign in"}
            variant="primary"
            full
            disabled={busy || code.trim() === ""}
            testId="console-login-submit"
            onClick={() => void submit()}
          />
        </div>
      </div>
    </div>
  );
}
