import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { getAgents, getConfig, type AgentOut } from "../api/client";
import { isWired } from "../api/config";

// Fallback workspace name shown while config is loading or when unwired.
const DEFAULT_ORG_NAME = "AgentOS";

// The real-data layer for wired mode. It fetches GET /agents and derives whether
// the account is fresh (onboarding) or has agents (live shell). The fixture demo
// (?state=N) never touches this: when not wired, the provider is inert and the
// app renders fixtures exactly as before. wired=real, unwired=fixtures, no mixing.

export interface JustDeployed {
  name: string;
  channel: string;
}

export interface WiredData {
  wired: boolean;
  agents: AgentOut[];
  /** Configurable org/workspace name from GET /config; falls back to a default. */
  orgName: string;
  loading: boolean;
  error: string | null;
  refetch: () => void;
  /** Set after a successful wired deploy so the shell can show the honest next step. */
  justDeployed: JustDeployed | null;
  markDeployed: (d: JustDeployed) => void;
  clearDeployed: () => void;
}

const Ctx = createContext<WiredData | null>(null);

export function WiredProvider({ children }: { children: ReactNode }) {
  const wired = isWired();
  const [agents, setAgents] = useState<AgentOut[]>([]);
  const [orgName, setOrgName] = useState(DEFAULT_ORG_NAME);
  const [loading, setLoading] = useState(wired);
  const [error, setError] = useState<string | null>(null);
  const [justDeployed, setJustDeployed] = useState<JustDeployed | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  const refetch = useCallback(() => setReloadKey((n) => n + 1), []);

  useEffect(() => {
    if (!wired) return;
    let live = true;
    setLoading(true);
    setError(null);
    getAgents()
      .then((data) => {
        if (!live) return;
        setAgents(data);
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (!live) return;
        setError(String(e));
        setLoading(false);
      });
    // Org name is chrome-only; a failure here must not block the agent list, so
    // it swallows errors and keeps the default rather than surfacing on `error`.
    getConfig()
      .then((cfg) => {
        if (live && cfg.org_name) setOrgName(cfg.org_name);
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [wired, reloadKey]);

  const value = useMemo<WiredData>(
    () => ({
      wired,
      agents,
      orgName,
      loading,
      error,
      refetch,
      justDeployed,
      markDeployed: (d) => setJustDeployed(d),
      clearDeployed: () => setJustDeployed(null),
    }),
    [wired, agents, orgName, loading, error, refetch, justDeployed],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useWired(): WiredData {
  const v = useContext(Ctx);
  if (!v) throw new Error("useWired must be used within WiredProvider");
  return v;
}
