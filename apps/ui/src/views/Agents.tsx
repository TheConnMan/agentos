import { C } from "../tokens";
import { Card, SectionTitle, Button, Dot, Chip, EmptyState } from "../primitives";
import { useStore } from "../state/store";
import { agentsForLevel } from "../fixtures";
import type { Agent } from "../fixtures";

function AgentCard({ agent }: { agent: Agent }) {
  const { state, dispatch } = useStore();
  const suite = agent.plugin ? 12 : state.extraEval && agent.id === "deal-desk" ? 37 : 36;
  const meta: [string, string | number][] = [
    ["version", agent.prodV],
    ["eval score", agent.score + "%"],
    ["runs today", agent.runs],
  ];
  return (
    <Card>
      <div style={{ display: "flex", alignItems: "center", gap: 9, marginBottom: 12 }}>
        <Dot color={C.brand} size={9} />
        <span style={{ fontFamily: C.mono, fontSize: 15, fontWeight: 500 }}>{agent.id}</span>
        {agent.plugin ? <Chip color={C.mutedStatus}>Plugin</Chip> : null}
        <span style={{ marginLeft: "auto", fontSize: 12, color: C.muted, fontFamily: C.mono }}>{agent.ch}</span>
      </div>
      <div
        style={{
          display: "flex",
          gap: 20,
          padding: "12px 0",
          borderTop: "1px solid " + C.border,
          borderBottom: "1px solid " + C.border,
          marginBottom: 12,
        }}
      >
        {meta.map((s, i) => (
          <div key={i}>
            <div style={{ fontSize: 11, color: C.muted, marginBottom: 3 }}>{s[0]}</div>
            <div style={{ fontFamily: C.mono, fontSize: 14 }}>{s[1]}</div>
          </div>
        ))}
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <Chip color={C.text2}>{suite + " eval cases"}</Chip>
        <button
          type="button"
          onClick={() => {
            dispatch({ type: "go", nav: "observability" });
            dispatch({ type: "setObsTab", tab: "traces" });
          }}
          style={{ marginLeft: "auto", background: "none", border: "none", color: C.link, fontSize: 13, cursor: "pointer" }}
        >
          View traces →
        </button>
      </div>
    </Card>
  );
}

export function Agents() {
  const { state, dispatch, slackOn } = useStore();

  if (state.level < 3) {
    return (
      <div>
        <SectionTitle title="Agents" />
        <EmptyState
          title="Create your first agent"
          sub={
            slackOn
              ? "Slack is connected. Create your first agent to put it to work."
              : "Write a skill.md in the browser and deploy it to Slack in seconds."
          }
          ctaLabel="New agent"
          onCta={() => dispatch({ type: "openModal", modal: "new-agent" })}
        />
      </div>
    );
  }

  const agents = agentsForLevel(state.level);
  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", marginBottom: 20 }}>
        <div>
          <SectionTitle title="Agents" />
        </div>
        <div style={{ marginLeft: "auto", display: "flex", gap: 10 }}>
          <Button label="Install plugin" onClick={() => dispatch({ type: "openModal", modal: "plugin" })} />
          <Button label="New agent" variant="primary" icon="+" onClick={() => dispatch({ type: "openModal", modal: "new-agent" })} />
        </div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(320px,1fr))", gap: 16 }}>
        {agents.map((a) => (
          <AgentCard key={a.id} agent={a} />
        ))}
      </div>
    </div>
  );
}
