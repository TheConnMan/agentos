import { C } from "../../tokens";
import { Button } from "../../primitives";
import { useStore } from "../../state/store";

// The classic create-agent modal: template picker + skill.md editor + Deploy.
// This is the MVP onboarding path (the interview wizard is deferred). Ported
// from the canon's newAgentModal(); Deploy runs the 700ms skeleton then fires
// the deploy action (confetti + success panel on Overview).
const TEMPLATES: [string, string, boolean][] = [
  ["Deal desk approvals", "revenue guardrails", true],
  ["SRE triage", "incident routing", false],
  ["Analytics Q&A", "ask your metrics", false],
  ["Blank skill.md", "start empty", false],
];

const SKILL = `---
name: deal-desk
description: Approves or routes sales-deal discount requests in Slack
tools: [salesforce-mcp, slack]
---

# When to run
Trigger when a teammate @-mentions the bot with a deal
approval request in #revenue-ops.

# Policy
- Read the deal from the CRM record, not the message.
- Auto-approve discounts up to and including 15%.
- Anything above 15% routes to the named approver.

# Hard rules
- Approver names come ONLY from policy.yaml — never invent one.
- Deal amounts come from the CRM record, never from the
  Slack message.
- If no matching CRM record exists, refuse and ask for the
  deal id. Do not guess.`;

export function NewAgentModal() {
  const { state, dispatch } = useStore();
  const lines = SKILL.split("\n");

  const deploy = () => {
    if (state.deploying) return;
    dispatch({ type: "deployStart" });
    setTimeout(() => dispatch({ type: "deployDone" }), 700);
  };

  return (
    <div
      style={{
        width: 820,
        maxWidth: "92vw",
        background: C.card,
        border: "1px solid " + C.borderStrong,
        borderRadius: 16,
        overflow: "hidden",
        display: "flex",
        flexDirection: "column",
        maxHeight: "86vh",
      }}
    >
      <div style={{ padding: "18px 24px", borderBottom: "1px solid " + C.border, display: "flex", alignItems: "center" }}>
        <h2 style={{ fontSize: 18, fontWeight: 500, margin: 0 }}>New agent</h2>
        <button
          type="button"
          onClick={() => dispatch({ type: "closeModal" })}
          style={{ marginLeft: "auto", background: "none", border: "none", color: C.muted, fontSize: 18, cursor: "pointer" }}
        >
          ✕
        </button>
      </div>
      <div style={{ display: "flex", gap: 0, flex: 1, minHeight: 0 }}>
        <div style={{ width: 300, flexShrink: 0, padding: "20px 24px", borderRight: "1px solid " + C.border, overflow: "auto" }}>
          <label style={{ fontSize: 13, fontWeight: 500, display: "block", marginBottom: 6 }}>Name</label>
          <input
            defaultValue="deal-desk"
            onFocus={(e) => e.target.select()}
            style={{
              width: "100%",
              background: C.input,
              border: "1px solid " + C.borderStrong,
              borderRadius: 7,
              padding: "8px 10px",
              color: C.text,
              fontFamily: C.mono,
              fontSize: 13,
              marginBottom: 20,
            }}
          />
          <label style={{ fontSize: 13, fontWeight: 500, display: "block", marginBottom: 8 }}>Template</label>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
            {TEMPLATES.map((t, i) => (
              <button
                key={i}
                type="button"
                style={{
                  textAlign: "left",
                  padding: "10px 11px",
                  borderRadius: 9,
                  cursor: "pointer",
                  background: t[2] ? "rgba(62,207,142,.08)" : C.input,
                  border: "1px solid " + (t[2] ? C.brand : C.border),
                  color: C.text,
                }}
              >
                <div style={{ fontSize: 12.5, fontWeight: 500, marginBottom: 2 }}>{t[0]}</div>
                <div style={{ fontSize: 11, color: C.muted }}>{t[1]}</div>
              </button>
            ))}
          </div>
        </div>
        <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
          <div
            style={{
              padding: "8px 14px",
              borderBottom: "1px solid " + C.border,
              fontFamily: C.mono,
              fontSize: 12,
              color: C.muted,
              display: "flex",
              alignItems: "center",
              gap: 8,
            }}
          >
            <span style={{ color: C.text2 }}>skills/deal-desk/skill.md</span>
          </div>
          <div style={{ flex: 1, overflow: "auto", background: C.darkest, display: "flex", fontFamily: C.mono, fontSize: 12.5, lineHeight: 1.55 }}>
            <div style={{ padding: "12px 8px", color: C.disabled, textAlign: "right", userSelect: "none", borderRight: "1px solid " + C.border }}>
              {lines.map((_, i) => (
                <div key={i}>{i + 1}</div>
              ))}
            </div>
            <pre style={{ margin: 0, padding: "12px 16px", color: C.text2, whiteSpace: "pre-wrap", flex: 1 }}>{SKILL}</pre>
          </div>
        </div>
      </div>
      <div style={{ padding: "14px 24px", borderTop: "1px solid " + C.border, display: "flex", alignItems: "center", gap: 12 }}>
        <span style={{ fontSize: 12, color: C.muted, fontFamily: C.mono }}>
          deploys to <span style={{ color: C.text2 }}>#revenue-ops</span>
        </span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 10 }}>
          <Button label="Cancel" variant="ghost" onClick={() => dispatch({ type: "closeModal" })} />
          {state.deploying ? (
            <div
              data-testid="deploy-skeleton"
              style={{
                width: 120,
                height: 36,
                borderRadius: 7,
                background: `linear-gradient(90deg,${C.hover} 0%,${C.sel} 40%,${C.hover} 80%)`,
                backgroundSize: "400px 100%",
                animation: "shimmer 1s linear infinite",
              }}
            />
          ) : (
            <Button label="Deploy" variant="primary" onClick={deploy} />
          )}
        </div>
      </div>
    </div>
  );
}
