import { C } from "../../tokens";
import { Card, Chip } from "../../primitives";
import { useStore } from "../../state/store";
import { ALL_AGENTS } from "../../fixtures";

export function Cost() {
  const { state, dispatch } = useStore();
  const fleet = state.level === 6;
  const rows: [string, string, string, string][] = fleet
    ? ALL_AGENTS.map((a) => [a.id, a.prodV, a.cost, "$" + (parseFloat(a.cost.slice(1)) / a.runs).toFixed(3)])
    : [["deal-desk", "v1.4.2", "$4.20", "$0.033"]];
  const grid = "1.4fr 1fr 1fr 1fr";
  return (
    <div>
      <div style={{ fontSize: 13, color: C.muted, marginBottom: 16 }}>{fleet ? "Per-agent spend · today" : "deal-desk · today"}</div>
      {fleet ? (
        <div
          style={{
            marginBottom: 16,
            padding: "14px 18px",
            background: "rgba(62,207,142,.06)",
            border: "1px solid rgba(62,207,142,.3)",
            borderRadius: 12,
            fontSize: 13.5,
            color: C.text2,
            display: "flex",
            gap: 10,
            alignItems: "center",
          }}
        >
          <span style={{ color: C.brand }}>◆</span>
          <div>
            <strong style={{ color: C.text }}>rev-analytics</strong>: identical eval score on a smaller model would save{" "}
            <strong style={{ color: C.brand }}>$310/mo</strong>.{" "}
            <button
              type="button"
              onClick={() => {
                dispatch({ type: "go", nav: "evals" });
                dispatch({ type: "setEvalTab", tab: "matrix" });
                dispatch({ type: "runMatrix" });
              }}
              style={{ color: C.link, background: "none", border: "none", cursor: "pointer", padding: 0, fontSize: 13.5 }}
            >
              Compare in Matrix →
            </button>
          </div>
        </div>
      ) : null}
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
          {["Agent", "Version", "Spend today", "Cost / interaction"].map((c) => (
            <div key={c}>{c}</div>
          ))}
        </div>
        {rows.map((r, i) => (
          <div
            key={i}
            style={{
              display: "grid",
              gridTemplateColumns: grid,
              gap: 12,
              padding: "12px 0",
              borderBottom: i < rows.length - 1 ? "1px solid " + C.border : "none",
              fontSize: 13.5,
              alignItems: "center",
            }}
          >
            <span style={{ fontFamily: C.mono, fontSize: 13 }}>{r[0]}</span>
            <div>
              <Chip color={C.brand} border="rgba(62,207,142,.4)">
                {r[1]}
              </Chip>
            </div>
            <span style={{ fontFamily: C.mono, color: C.text2 }}>{r[2]}</span>
            <span style={{ fontFamily: C.mono, color: C.text2 }}>{r[3]}</span>
          </div>
        ))}
      </Card>
    </div>
  );
}
