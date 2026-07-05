import { C } from "../../tokens";
import { Card } from "../../primitives";
import { useStore } from "../../state/store";
import { USAGE_SINGLE, USAGE_FLEET } from "../../fixtures";

export function Usage() {
  const { state, dispatch } = useStore();
  const fleet = state.level === 6;
  const data = fleet ? USAGE_FLEET : USAGE_SINGLE;
  const users = data.users;
  const intents = data.intents;
  const mx = Math.max(...users.map((u) => u[1]));
  return (
    <div>
      <div style={{ fontSize: 13, color: C.muted, marginBottom: 16 }}>
        {fleet ? "Fleet-wide · last 7 days" : "deal-desk · last 7 days"}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: fleet ? "1fr 1fr" : "1fr", gap: 16 }}>
        <Card>
          <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 16 }}>Top users</div>
          {users.map((u, i) => (
            <div key={i} style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12 }}>
              <span style={{ width: 60, fontSize: 13, fontFamily: C.mono, color: C.text2 }}>{u[0]}</span>
              <div style={{ flex: 1, height: 8, background: C.input, borderRadius: 4, overflow: "hidden" }}>
                <div style={{ width: (u[1] / mx) * 100 + "%", height: "100%", background: C.brand, borderRadius: 4 }} />
              </div>
              <span style={{ width: 40, textAlign: "right", fontFamily: C.mono, fontSize: 12.5, color: C.muted }}>{u[1]}</span>
            </div>
          ))}
        </Card>
        <Card>
          <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 16 }}>Top intents</div>
          {intents.map((it, i) => (
            <div
              key={i}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 12,
                padding: "9px 0",
                borderBottom: i < intents.length - 1 ? "1px solid " + C.border : "none",
              }}
            >
              <span style={{ flex: 1, fontSize: 13, color: C.text2 }}>{"“" + it[0] + "”"}</span>
              <span style={{ fontFamily: C.mono, fontSize: 12.5, color: C.muted }}>{it[1]}</span>
            </div>
          ))}
        </Card>
      </div>
      {fleet ? (
        <div style={{ marginTop: 16 }}>
          <Card>
            <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 14 }}>Human overrides</div>
            {USAGE_FLEET.overrides.map((o, i) => (
              <div
                key={i}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 12,
                  padding: "8px 0",
                  borderBottom: i < 2 ? "1px solid " + C.border : "none",
                }}
              >
                <span style={{ width: 120, fontFamily: C.mono, fontSize: 12.5 }}>{o[0]}</span>
                <span style={{ flex: 1, fontSize: 13, color: C.text2 }}>
                  humans overrode <strong style={{ color: o[1] === "11%" ? C.warn : C.text }}>{o[1]}</strong> of verdicts this week
                </span>
                <button
                  type="button"
                  onClick={() => dispatch({ type: "setObsTab", tab: "traces" })}
                  style={{ color: C.link, fontSize: 12.5, background: "none", border: "none", cursor: "pointer" }}
                >
                  view runs →
                </button>
              </div>
            ))}
          </Card>
        </div>
      ) : null}
    </div>
  );
}
