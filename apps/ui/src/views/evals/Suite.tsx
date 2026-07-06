import { C } from "../../tokens";
import { Card, Chip, Dot, Button } from "../../primitives";
import { useStore } from "../../state/store";
import { EVAL_CASES } from "../../fixtures";
import type { EvalCase } from "../../fixtures";

export function Suite() {
  const { state, dispatch } = useStore();
  const total = state.extraEval ? 37 : 36;
  const passed = state.extraEval ? 35 : 34;
  const cases: EvalCase[] = state.extraEval
    ? [...EVAL_CASES, { n: "approver-from-policy-source·promoted", s: [1, 1, 1, 1], dur: "0.7s" }]
    : EVAL_CASES;
  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 14, marginBottom: 8 }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 500, fontFamily: C.mono }}>deal-desk core</div>
          <div style={{ fontSize: 12.5, color: C.muted }}>
            {total + " cases · last run 3h ago on dev@4f2c91a · claude-sonnet-5"}
          </div>
        </div>
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 10 }}>
          <Chip color={C.success} border="rgba(46,160,67,.4)" pre={<Dot color={C.success} size={7} />}>
            {passed + " of " + total + " checks passed"}
          </Chip>
          <Button label="Run suite" onClick={() => dispatch({ type: "toast", message: "Suite queued" })} />
        </div>
      </div>
      <div style={{ fontSize: 12.5, color: C.muted, marginBottom: 16 }}>
        Each case is a known input with an expected guardrail outcome. A case fails when the current skill.md + model
        deviates.
      </div>
      <Card>
        {cases.map((c, i) => {
          const pass = c.s[1] !== 0;
          const dur = c.dur ?? (0.6 + ((i * 0.13) % 1.4)).toFixed(1) + "s";
          return (
            <div
              key={c.n}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 12,
                padding: "10px 0",
                borderBottom: i < cases.length - 1 ? "1px solid " + C.border : "none",
              }}
            >
              <span style={{ fontSize: 11, fontWeight: 700, color: pass ? C.success : C.failure, minWidth: 14 }}>
                {pass ? "✓" : "✗"}
              </span>
              <span style={{ flex: 1, fontFamily: C.mono, fontSize: 12.5, color: C.text2 }}>{c.n}</span>
              <Chip
                color={pass ? C.success : C.failure}
                border={pass ? "rgba(46,160,67,.35)" : "rgba(207,34,41,.4)"}
              >
                {pass ? "pass" : "fail"}
              </Chip>
              <span style={{ fontFamily: C.mono, fontSize: 12, color: C.muted, width: 44, textAlign: "right" }}>{dur}</span>
            </div>
          );
        })}
        <div style={{ padding: "12px 0 0", fontSize: 12.5, color: C.muted, fontFamily: C.mono }}>…30 more cases</div>
      </Card>
    </div>
  );
}
