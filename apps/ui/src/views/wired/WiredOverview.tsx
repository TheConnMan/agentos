import type { ReactNode } from "react";
import { C } from "../../tokens";
import { Card, SectionTitle, Button, Dot } from "../../primitives";
import { hoverBg } from "../../lib/style";
import { useStore } from "../../state/store";
import { useWired } from "../../state/wired";
import { useMetricsSummary, useTraces } from "../../api/hooks";
import { ConnectSlackPanel } from "../../components/ConnectSlackPanel";
import type { AgentOut, RawTrace } from "../../api/client";

function Notice({ children }: { children: ReactNode }) {
  return <div style={{ padding: "30px 20px", textAlign: "center", color: C.muted, fontSize: 13 }}>{children}</div>;
}

// Honest post-deploy panel: the real next step, not a fictional "replied in 42ms".
function DeployedPanel({ name, channel }: { name: string; channel: string }) {
  return (
    <div
      data-testid="deployed-panel"
      style={{
        background: "rgba(62,207,142,.07)",
        border: "1px solid rgba(62,207,142,.3)",
        borderRadius: 14,
        padding: "18px 20px",
        marginBottom: 20,
        display: "flex",
        alignItems: "flex-start",
        gap: 14,
      }}
    >
      <div
        style={{
          width: 34,
          height: 34,
          borderRadius: 9,
          background: "rgba(62,207,142,.15)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: C.brand,
          fontSize: 16,
          flexShrink: 0,
        }}
      >
        ✓
      </div>
      <div>
        <div style={{ fontSize: 15, fontWeight: 500, marginBottom: 3 }}>
          <span style={{ fontFamily: C.mono, color: C.brand }}>{name}</span> is deployed
        </div>
        <div style={{ fontSize: 13.5, color: C.text2, lineHeight: 1.5 }}>
          Next: invite your Slack bot to <span style={{ fontFamily: C.mono, color: C.text }}>{channel}</span> and mention
          it there. Its runs, traces, and cost light up in Observability as soon as it handles a message.
        </div>
      </div>
    </div>
  );
}

function Onboarding() {
  const { dispatch } = useStore();
  return (
    <div>
      <SectionTitle title="Welcome to AgentOS" sub="Get your first agent live in Slack." />
      <div style={{ display: "flex", flexDirection: "column", gap: 16, maxWidth: 720 }}>
        <ConnectSlackPanel />
        <Card>
          <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 15, fontWeight: 500, marginBottom: 3 }}>Create your first agent</div>
              <div style={{ fontSize: 13, color: C.muted }}>
                Write a skill.md, pick a channel, and deploy. You will see it here the moment it exists.
              </div>
            </div>
            <Button label="New agent" variant="primary" icon="+" onClick={() => dispatch({ type: "openModal", modal: "new-agent" })} />
          </div>
        </Card>
      </div>
    </div>
  );
}

function traceMsg(t: RawTrace): string {
  const name = t["name"];
  const input = t["input"];
  if (typeof name === "string" && name) return name;
  if (typeof input === "string" && input) return input;
  return String(t["id"] ?? "trace");
}

function LiveOverview({ agents }: { agents: AgentOut[] }) {
  const { dispatch } = useStore();
  const { justDeployed } = useWired();
  const summary = useMetricsSummary(true, {});
  const traces = useTraces(true);

  const stat = (label: string, value: string) => (
    <Card key={label} style={{ padding: "16px 18px" }}>
      <div style={{ fontSize: 12, color: C.muted, marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: 24, fontWeight: 400, fontFamily: C.mono }}>{value}</div>
    </Card>
  );
  const s = summary.data;
  const stats: [string, string][] = [
    ["Agents", String(agents.length)],
    ["Runs (7d)", s ? String(s.runs) : summary.loading ? "…" : "0"],
    ["Latency p95", s ? s.latency_p95_seconds.toFixed(2) + "s" : "—"],
    ["Cost (7d)", s ? "$" + s.cost_usd.toFixed(2) : "—"],
  ];
  const recent = traces.data ?? [];

  return (
    <div>
      {justDeployed ? <DeployedPanel name={justDeployed.name} channel={justDeployed.channel} /> : null}
      <SectionTitle title="Overview" sub={`${agents.length} ${agents.length === 1 ? "agent" : "agents"} · live data`} />
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 14, marginBottom: 22 }}>
        {stats.map((x) => stat(x[0], x[1]))}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1.5fr 1fr", gap: 16 }}>
        <Card>
          <div style={{ display: "flex", alignItems: "center", marginBottom: 8 }}>
            <div style={{ fontSize: 14, fontWeight: 500 }}>Recent activity</div>
            <button
              type="button"
              onClick={() => {
                dispatch({ type: "go", nav: "observability" });
                dispatch({ type: "setObsTab", tab: "traces" });
              }}
              style={{ marginLeft: "auto", background: "none", border: "none", color: C.link, fontSize: 12.5, cursor: "pointer" }}
            >
              All traces →
            </button>
          </div>
          {traces.loading ? (
            <Notice>Loading…</Notice>
          ) : recent.length === 0 ? (
            <Notice>No runs yet. Mention an agent in Slack to generate the first trace.</Notice>
          ) : (
            recent.slice(0, 5).map((t, i) => (
              <div
                key={String(t["id"] ?? i)}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                  padding: "11px 0",
                  borderTop: i ? "1px solid " + C.border : "none",
                }}
              >
                <Dot color={C.success} size={7} />
                <span style={{ flex: 1, fontSize: 13, color: C.text2, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                  {traceMsg(t)}
                </span>
              </div>
            ))
          )}
        </Card>
        <Card>
          <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 14 }}>Agents</div>
          {agents.map((a, i) => (
            <button
              key={a.id}
              type="button"
              onClick={() => dispatch({ type: "go", nav: "agents" })}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
                padding: "10px 0",
                width: "100%",
                textAlign: "left",
                background: "transparent",
                border: "none",
                borderTop: i ? "1px solid " + C.border : "none",
                cursor: "pointer",
                color: C.text,
              }}
              {...hoverBg("transparent", C.hover)}
            >
              <Dot color={C.success} size={8} />
              <span style={{ flex: 1, fontFamily: C.mono, fontSize: 13 }}>{a.name}</span>
              <span style={{ fontSize: 12, color: C.muted, fontFamily: C.mono }}>{a.slack_channel}</span>
            </button>
          ))}
        </Card>
      </div>
    </div>
  );
}

export function WiredOverview() {
  const { agents, loading, error } = useWired();
  if (loading) return <Notice>Loading your workspace…</Notice>;
  if (error) return <Notice>Could not reach the backend: {error}</Notice>;
  if (agents.length === 0) return <Onboarding />;
  return <LiveOverview agents={agents} />;
}
