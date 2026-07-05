import { C } from "../../tokens";
import { Button } from "../../primitives";
import { useStore } from "../../state/store";

const PERMS = [
  "Send messages as @agentos",
  "Read messages in channels it is added to",
  "View basic workspace & user info",
  "Post to #revenue-ops and other channels",
];

// Slack OAuth-grant modal. "Allow" connects Slack (advances to level 2). Ported
// from slackModal().
export function SlackModal() {
  const { dispatch } = useStore();
  return (
    <div style={{ width: 420, background: C.card, border: "1px solid " + C.borderStrong, borderRadius: 16, overflow: "hidden" }}>
      <div style={{ padding: "20px 24px 16px", borderBottom: "1px solid " + C.border }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
          <div
            style={{
              width: 26,
              height: 26,
              borderRadius: 7,
              background: C.sel,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 13,
            }}
          >
            #
          </div>
          <span style={{ fontSize: 12, color: C.muted, fontFamily: C.mono }}>acme-corp.slack.com</span>
        </div>
        <h2 style={{ fontSize: 18, fontWeight: 500, margin: 0 }}>AgentOS is requesting access</h2>
      </div>
      <div style={{ padding: "18px 24px" }}>
        <div style={{ fontSize: 12, color: C.muted, marginBottom: 12, letterSpacing: 0.3 }}>THIS WILL ALLOW AGENTOS TO</div>
        {PERMS.map((p, i) => (
          <div key={i} style={{ display: "flex", gap: 10, alignItems: "flex-start", padding: "7px 0", fontSize: 13.5, color: C.text2 }}>
            <span style={{ color: C.brand }}>✓</span> {p}
          </div>
        ))}
      </div>
      <div style={{ padding: "14px 24px", borderTop: "1px solid " + C.border, display: "flex", gap: 10, justifyContent: "flex-end" }}>
        <Button label="Cancel" variant="ghost" onClick={() => dispatch({ type: "closeModal" })} />
        <Button label="Allow" variant="primary" onClick={() => dispatch({ type: "allowSlack" })} />
      </div>
    </div>
  );
}
