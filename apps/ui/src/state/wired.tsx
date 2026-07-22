import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { getAgents, getConfig, type AgentOut } from "../api/client";

// Fallback workspace name shown while config is loading.
const DEFAULT_ORG_NAME = "AgentOS";

// The real-data layer for the console. It fetches GET /agents and derives whether
// the account is fresh (onboarding) or has agents (live shell), plus the
// workspace name from GET /config.

export interface JustDeployed {
  name: string;
  channel: string;
}

export interface WiredData {
  /** Always true now that the console is backed by the live API; retained as the
   * enabled flag threaded into the react-query hooks. */
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
  const [agents, setAgents] = useState<AgentOut[]>([]);
  const [orgName, setOrgName] = useState(DEFAULT_ORG_NAME);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [justDeployed, setJustDeployed] = useState<JustDeployed | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  const refetch = useCallback(() => setReloadKey((n) => n + 1), []);

  useEffect(() => {
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
  }, [reloadKey]);

  const value = useMemo<WiredData>(
    () => ({
      wired: true,
      agents,
      orgName,
      loading,
      error,
      refetch,
      justDeployed,
      markDeployed: (d) => setJustDeployed(d),
      clearDeployed: () => setJustDeployed(null),
    }),
    [agents, orgName, loading, error, refetch, justDeployed],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useWired(): WiredData {
  const v = useContext(Ctx);
  if (!v) throw new Error("useWired must be used within WiredProvider");
  return v;
}
