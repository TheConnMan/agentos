import { useCallback, useEffect, useState, type ReactNode } from "react";
import { isWired } from "../api/config";
import { activateSession, getSession } from "../api/client";
import { ConsoleLogin } from "./ConsoleLogin";

// The wired console's auth gate (#630 / ADR-0049).
//
// It is a real state transition, not an overlay: while locked, the console
// shell is not rendered at all, so nothing behind it mounts, fetches, or 401s.
// That is why this sits above StoreProvider/WiredProvider in main.tsx rather
// than inside the shell.
//
// Fixture mode (no ?api=1) is untouched: the gate opens immediately and never
// calls the session endpoint, so the stackless demo stays backend-free.

type Phase = "checking" | "locked" | "open";

export function ConsoleGate({ children }: { children: ReactNode }) {
  const wired = isWired();
  const [phase, setPhase] = useState<Phase>(wired ? "checking" : "open");

  useEffect(() => {
    if (!wired) return;
    let live = true;
    getSession()
      .then((session) => {
        if (live) setPhase(session.authenticated ? "open" : "locked");
      })
      // Fail closed: if the session state cannot be read, show the login view
      // rather than a shell whose every call is about to be rejected.
      .catch(() => {
        if (live) setPhase("locked");
      });
    return () => {
      live = false;
    };
  }, [wired]);

  // Throws on a rejected code, which ConsoleLogin renders inline; a non-2xx has
  // already thrown by the time this resolves, so success is the only path here.
  const activate = useCallback(async (code: string) => {
    await activateSession(code);
    setPhase("open");
  }, []);

  // Nothing is rendered until the session state is known: showing the login
  // view first would flash a gate at an operator who is already signed in.
  if (phase === "checking") return null;
  if (phase === "locked") return <ConsoleLogin onActivate={activate} />;
  return <>{children}</>;
}
