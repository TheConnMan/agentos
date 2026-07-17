import { useState } from "react";
import { C, R } from "../tokens";
import { Button } from "../primitives";
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
          <p style={{ fontSize: 13, color: C.muted, lineHeight: 1.5, margin: "0 0 20px" }}>
            {/*
              The exact `agentos ... console login` invocation is deliberately not
              written out here. Per apps/ui/CLAUDE.md every agentos command string
              in a component must resolve from the committed manifest through
              cliCommand(), and the console login verb is not in the manifest yet
              (it is the CLI half of #630). Once `pnpm gen:manifest` picks it up
              this paragraph gets a CliHint next to it.
            */}
            Run the AgentOS CLI&rsquo;s console login command on this install to mint a login code, then paste it
            below. Codes are single use and expire.
          </p>

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
