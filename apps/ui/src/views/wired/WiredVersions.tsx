import { useState, type ReactNode } from "react";
import { C } from "../../tokens";
import { Card, SectionTitle, Chip, Dot } from "../../primitives";
import { useAgents, useAgentVersions } from "../../api/hooks";
import { ComingSoon } from "./WiredStubs";
import type { DeploymentOut, VersionOut } from "../../api/client";

function Notice({ children }: { children: ReactNode }) {
  return <div style={{ padding: "30px 20px", textAlign: "center", color: C.muted, fontSize: 13 }}>{children}</div>;
}

export interface Row {
  version: VersionOut;
  deployment: DeploymentOut | null;
}

// Flatten versions x their deployments: one row per deployment, plus a row for
// any version that was never deployed. Newest activity first.
export function buildRows(versions: VersionOut[], deployments: DeploymentOut[]): Row[] {
  const rows: Row[] = versions.flatMap((version): Row[] => {
    const deps = deployments.filter((d) => d.version_id === version.id);
    if (deps.length === 0) return [{ version, deployment: null }];
    return deps.map((deployment) => ({ version, deployment }));
  });
  return rows.sort((a, b) => {
    const at = a.deployment?.deployed_at ?? a.version.created_at;
    const bt = b.deployment?.deployed_at ?? b.version.created_at;
    return bt.localeCompare(at);
  });
}

function statusColor(status: string | null): string {
  if (status === "active") return C.success;
  if (status === null) return C.mutedStatus;
  return C.warn;
}

// Versions table for one agent: real versions joined with their deployments
// (environment, status, deployed_at). No Eval column. Agent-scoped via a
// selector, matching the Cost view. Falls back to ComingSoon when the API is
// unreachable (there is no fixture leak in wired mode).
function VersionsTable({ agentId }: { agentId: string }) {
  const { versions, deployments, activeVersionId, loading, error } = useAgentVersions(agentId);

  if (error) {
    return (
      <ComingSoon
        title="Versions are not available"
        body={`Could not load versions from the backend: ${error}`}
      />
    );
  }
  if (loading) return <Notice>Loading versions…</Notice>;
  if (versions.length === 0) {
    return (
      <ComingSoon
        title="No versions yet"
        body="Deploy this agent or push to a connected git branch and its versions appear here."
      />
    );
  }

  const rows = buildRows(versions, deployments);
  const grid = "1.1fr 0.9fr 1fr 1.3fr 1fr";
  return (
    <Card>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: grid,
          gap: 12,
          padding: "0 0 12px",
          fontSize: 12,
          color: C.muted,
          borderBottom: "1px solid " + C.border,
        }}
      >
        {["Version", "Environment", "Status", "Deployed", "Created by"].map((c) => (
          <div key={c}>{c}</div>
        ))}
      </div>
      {rows.map((r, i) => {
        const env = r.deployment?.environment ?? null;
        const isActive = r.deployment?.status === "active" && r.version.id === activeVersionId;
        return (
          <div
            key={`${r.version.id}-${r.deployment?.id ?? "none"}-${i}`}
            data-testid="version-row"
            style={{
              display: "grid",
              gridTemplateColumns: grid,
              gap: 12,
              padding: "13px 0",
              alignItems: "center",
              borderBottom: "1px solid " + C.border,
              fontSize: 13.5,
            }}
          >
            <span style={{ fontFamily: C.mono, fontSize: 12.5, display: "flex", alignItems: "center", gap: 8 }}>
              {r.version.version_label}
              {isActive ? <Chip color={C.brand} border="rgba(62,207,142,.4)">active</Chip> : null}
            </span>
            <div>
              {env ? (
                <Chip
                  color={env === "prod" ? C.brand : C.warn}
                  border={env === "prod" ? "rgba(62,207,142,.4)" : "rgba(191,135,0,.4)"}
                >
                  {env}
                </Chip>
              ) : (
                <span style={{ color: C.muted }}>—</span>
              )}
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
              <Dot color={statusColor(r.deployment?.status ?? null)} size={7} />
              <span style={{ fontSize: 12.5, color: C.text2 }}>{r.deployment?.status ?? "not deployed"}</span>
            </div>
            <span style={{ color: C.muted, fontFamily: C.mono, fontSize: 12 }}>
              {r.deployment ? new Date(r.deployment.deployed_at).toLocaleString() : "—"}
            </span>
            <span style={{ color: C.text2, fontFamily: C.mono, fontSize: 12 }}>{r.version.created_by}</span>
          </div>
        );
      })}
    </Card>
  );
}

export function WiredVersions() {
  const agents = useAgents(true);
  const [selected, setSelected] = useState<string>("");

  if (agents.error) {
    return (
      <div>
        <SectionTitle title="Versions" sub="main → your prod bot · dev → your dev bot." />
        <ComingSoon
          title="Versions are not available"
          body={`Could not load agents from the backend: ${agents.error}`}
        />
      </div>
    );
  }
  if (agents.loading) {
    return (
      <div>
        <SectionTitle title="Versions" sub="main → your prod bot · dev → your dev bot." />
        <Notice>Loading agents…</Notice>
      </div>
    );
  }

  const list = agents.data ?? [];
  if (list.length === 0) {
    return (
      <div>
        <SectionTitle title="Versions" sub="main → your prod bot · dev → your dev bot." />
        <ComingSoon
          title="No versions deployed yet"
          body="Deploy an agent or push to a connected git branch and its versions appear here. This workspace has none yet."
        />
      </div>
    );
  }

  const activeId = selected || list[0]?.id || "";

  return (
    <div>
      <SectionTitle title="Versions" sub="main → your prod bot · dev → your dev bot." />
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16 }}>
        <span style={{ fontSize: 12.5, color: C.muted }}>Agent</span>
        <select
          data-testid="versions-agent-select"
          value={activeId}
          onChange={(e) => setSelected(e.target.value)}
          style={{
            background: C.input,
            border: "1px solid " + C.borderStrong,
            borderRadius: 7,
            padding: "7px 10px",
            color: C.text,
            fontFamily: C.mono,
            fontSize: 13,
          }}
        >
          {list.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
      </div>
      <VersionsTable key={activeId} agentId={activeId} />
    </div>
  );
}
