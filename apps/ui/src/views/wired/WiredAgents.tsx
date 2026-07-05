import { C } from "../../tokens";
import { Card, SectionTitle, Button, Dot, Chip, EmptyState } from "../../primitives";
import { useStore } from "../../state/store";
import { useWired } from "../../state/wired";

function Notice({ children }: { children: string }) {
  return <div style={{ padding: "30px 20px", textAlign: "center", color: C.muted, fontSize: 13 }}>{children}</div>;
}

export function WiredAgents() {
  const { dispatch } = useStore();
  const { agents, loading, error } = useWired();

  if (loading) return <Notice>Loading agents…</Notice>;
  if (error) return <Notice>{`Could not load agents: ${error}`}</Notice>;

  if (agents.length === 0) {
    return (
      <div>
        <SectionTitle title="Agents" />
        <EmptyState
          title="Create your first agent"
          sub="Write a skill.md in the browser, pick a channel, and deploy it to Slack in seconds."
          ctaLabel="New agent"
          onCta={() => dispatch({ type: "openModal", modal: "new-agent" })}
          showDemo={false}
        />
      </div>
    );
  }

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", marginBottom: 20 }}>
        <div>
          <SectionTitle title="Agents" />
        </div>
        <div style={{ marginLeft: "auto" }}>
          <Button label="New agent" variant="primary" icon="+" onClick={() => dispatch({ type: "openModal", modal: "new-agent" })} />
        </div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(320px,1fr))", gap: 16 }}>
        {agents.map((a) => (
          <Card key={a.id}>
            <div style={{ display: "flex", alignItems: "center", gap: 9, marginBottom: 12 }}>
              <Dot color={C.brand} size={9} />
              <span style={{ fontFamily: C.mono, fontSize: 15, fontWeight: 500 }} data-testid="agent-card-name">
                {a.name}
              </span>
              <span style={{ marginLeft: "auto", fontSize: 12, color: C.muted, fontFamily: C.mono }}>{a.slack_channel}</span>
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
              <div>
                <div style={{ fontSize: 11, color: C.muted, marginBottom: 3 }}>channel</div>
                <div style={{ fontFamily: C.mono, fontSize: 13 }}>{a.slack_channel}</div>
              </div>
              <div>
                <div style={{ fontSize: 11, color: C.muted, marginBottom: 3 }}>created</div>
                <div style={{ fontFamily: C.mono, fontSize: 13 }}>{new Date(a.created_at).toLocaleDateString()}</div>
              </div>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <Chip color={C.mutedStatus} border={C.border}>
                live
              </Chip>
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
        ))}
      </div>
    </div>
  );
}
