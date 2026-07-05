import type { CSSProperties } from "react";
import { C } from "../../tokens";
import { Card, Chip, Dot, Button } from "../../primitives";
import { useStore } from "../../state/store";
import { EVAL_CASES, MATRIX_VERSIONS, MATRIX_BEST, MATRIX_WORST } from "../../fixtures";

// Per-column tint: worst column red-washed, best column green-washed with rails.
function cellCol(idx: number): CSSProperties {
  return {
    background: idx === MATRIX_WORST ? "rgba(207,34,41,.06)" : idx === MATRIX_BEST ? "rgba(62,207,142,.04)" : "transparent",
    borderLeft: idx === MATRIX_BEST ? "1px solid rgba(62,207,142,.5)" : "1px solid transparent",
    borderRight: idx === MATRIX_BEST ? "1px solid rgba(62,207,142,.5)" : "1px solid transparent",
  };
}

const GRID = "1.7fr repeat(4,1fr)";

export function Matrix() {
  const { state, dispatch } = useStore();

  if (!state.matrixRun) {
    return (
      <div>
        <div style={{ fontSize: 14, color: C.text2, marginBottom: 16, maxWidth: 600 }}>
          Run one suite against multiple (version × model) combinations side by side. Bisect regressions, and see whether
          a cheaper model holds the same score.
        </div>
        <Card style={{ maxWidth: 620 }}>
          <div style={{ fontSize: 12, color: C.muted, marginBottom: 8 }}>SUITE</div>
          <div style={{ marginBottom: 18 }}>
            <Chip color={C.text} border={C.borderStrong}>
              deal-desk core
            </Chip>
          </div>
          <div style={{ fontSize: 12, color: C.muted, marginBottom: 8 }}>VERSION × MODEL</div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 22 }}>
            {MATRIX_VERSIONS.map((v) => (
              <span
                key={v.label}
                style={{
                  fontFamily: C.mono,
                  fontSize: 12,
                  padding: "5px 11px",
                  borderRadius: 20,
                  border: "1px solid " + (v.col > 1 ? "rgba(191,135,0,.4)" : C.borderStrong),
                  color: v.col > 1 ? C.warn : C.text2,
                  background: "rgba(62,207,142,.04)",
                }}
              >
                {"✓ " + v.label}
              </span>
            ))}
          </div>
          <Button label="Run matrix" variant="primary" onClick={() => dispatch({ type: "runMatrix" })} />
        </Card>
      </div>
    );
  }

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", marginBottom: 16 }}>
        <div style={{ fontSize: 14, color: C.text2 }}>
          Suite <span style={{ fontFamily: C.mono, color: C.text }}>deal-desk core</span>
          {" · " + EVAL_CASES.length + " cases × 4 versions"}
        </div>
        <button
          type="button"
          onClick={() => dispatch({ type: "reconfigureMatrix" })}
          style={{ marginLeft: "auto", background: "none", border: "none", color: C.link, fontSize: 13, cursor: "pointer" }}
        >
          Reconfigure
        </button>
      </div>
      <Card>
        <div style={{ overflowX: "auto" }}>
          <div style={{ minWidth: 640 }}>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: GRID,
                gap: 8,
                paddingBottom: 12,
                borderBottom: "1px solid " + C.border,
                alignItems: "end",
              }}
            >
              <div style={{ fontSize: 12, color: C.muted }}>case</div>
              {MATRIX_VERSIONS.map((v) => (
                <div
                  key={v.col}
                  style={{
                    textAlign: "center",
                    padding: "10px 6px 8px",
                    borderRadius: "8px 8px 0 0",
                    ...cellCol(v.col),
                    borderBottom: "none",
                  }}
                >
                  <div
                    style={{
                      fontSize: 22,
                      fontFamily: C.mono,
                      color: v.col === MATRIX_WORST ? C.failure : v.col === MATRIX_BEST ? C.brand : C.text,
                    }}
                  >
                    {v.score + "%"}
                  </div>
                  <div style={{ fontSize: 10.5, fontFamily: C.mono, color: C.muted, marginTop: 3, whiteSpace: "nowrap" }}>
                    {v.label}
                  </div>
                </div>
              ))}
            </div>
            {EVAL_CASES.map((c) => (
              <div
                key={c.n}
                style={{
                  display: "grid",
                  gridTemplateColumns: GRID,
                  gap: 8,
                  padding: "9px 0",
                  borderBottom: "1px solid " + C.border,
                  alignItems: "center",
                }}
              >
                <span style={{ fontFamily: C.mono, fontSize: 12, color: C.text2 }}>{c.n}</span>
                {c.s.map((val, ci) => (
                  <div key={ci} style={{ display: "flex", justifyContent: "center", padding: "6px", ...cellCol(ci) }}>
                    {val ? (
                      <Dot color={C.success} size={10} />
                    ) : (
                      <span style={{ width: 10, height: 10, borderRadius: "50%", border: "1.5px solid " + C.failure, display: "inline-block" }} />
                    )}
                  </div>
                ))}
              </div>
            ))}
          </div>
        </div>
        <div
          style={{
            marginTop: 16,
            padding: "12px 14px",
            background: "rgba(207,34,41,.06)",
            border: "1px solid rgba(207,34,41,.28)",
            borderRadius: 10,
            fontSize: 13,
            color: C.text2,
            display: "flex",
            gap: 10,
            alignItems: "flex-start",
          }}
        >
          <span style={{ color: C.failure }}>▲</span>
          <div>
            <strong style={{ color: C.text }}>2 regressions introduced after 4f2c91a</strong> — likely cause: skill.md
            change in commit <span style={{ fontFamily: C.mono, color: C.text }}>b7e02d1</span>.
          </div>
        </div>
      </Card>
    </div>
  );
}
