import { C } from "../tokens";
import { Card, Button, Dot, Chip } from "../primitives";
import { healthColor } from "../primitives";
import { useStore } from "../state/store";
import { ALL_AGENTS, DRIFT_SERIES, DRIFT_MARKERS } from "../fixtures";

// The 30-day eval pass-rate drift chart with version-deploy markers. Ported from
// driftPanel(); the dip visibly starts at the v2.1.0 model-update marker.
function DriftPanel() {
  const { state, dispatch } = useStore();
  const W = 760;
  const H = 220;
  const pad = 30;
  const data = DRIFT_SERIES;
  const mn = 78;
  const mx = 100;
  const x = (i: number) => pad + (i / (data.length - 1)) * (W - pad * 2);
  const y = (v: number) => pad + (1 - (v - mn) / (mx - mn)) * (H - pad * 2);
  const path = data.map((v, i) => `${x(i)},${y(v)}`).join(" ");
  return (
    <div>
      <Card>
        <div style={{ display: "flex", alignItems: "center", marginBottom: 2 }}>
          <div style={{ fontSize: 14, fontWeight: 500 }}>Eval suite pass-rate by deploy · 30 days</div>
          <div style={{ marginLeft: "auto" }}>
            <Button
              label="Compare in Matrix"
              size="sm"
              onClick={() => {
                dispatch({ type: "closeAgentDetail" });
                dispatch({ type: "go", nav: "evals" });
                dispatch({ type: "setEvalTab", tab: "matrix" });
                dispatch({ type: "runMatrix" });
              }}
            />
          </div>
        </div>
        <div style={{ fontSize: 12, color: C.muted, marginBottom: 12 }}>
          Each point re-runs the suite against that deploy — a proxy for capability drift, not live traffic.
        </div>
        <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ display: "block" }}>
          {[80, 90, 100].map((g) => (
            <g key={g}>
              <line x1={pad} x2={W - pad} y1={y(g)} y2={y(g)} stroke={C.border} strokeWidth={1} />
              <text x={4} y={y(g) + 4} fill={C.muted} fontSize={10} fontFamily={C.mono}>
                {g + "%"}
              </text>
            </g>
          ))}
          {DRIFT_MARKERS.map((m, i) => (
            <g
              key={i}
              style={{ cursor: "pointer" }}
              onMouseEnter={() => dispatch({ type: "setDriftHover", label: m.label })}
              onMouseLeave={() => dispatch({ type: "setDriftHover", label: null })}
            >
              <line
                x1={x(m.i)}
                x2={x(m.i)}
                y1={pad}
                y2={H - pad}
                stroke={m.bad ? "rgba(207,34,41,.5)" : C.borderStrong}
                strokeWidth={1}
                strokeDasharray="3 3"
              />
              <circle cx={x(m.i)} cy={pad} r={3.5} fill={m.bad ? C.failure : C.mutedStatus} />
            </g>
          ))}
          <polyline points={path} fill="none" stroke={C.warn} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
          <circle cx={x(16)} cy={y(data[16])} r={4} fill={C.failure} />
        </svg>
        <div style={{ fontSize: 12.5, color: state.driftHover ? C.text2 : C.muted, marginTop: 8, fontFamily: C.mono, minHeight: 18 }}>
          {state.driftHover ? "marker: " + state.driftHover : "hover a version marker for details"}
        </div>
      </Card>
      <div
        style={{
          marginTop: 16,
          padding: "14px 18px",
          background: "rgba(191,135,0,.07)",
          border: "1px solid rgba(191,135,0,.3)",
          borderRadius: 12,
          fontSize: 13.5,
          color: C.text2,
          display: "flex",
          gap: 10,
          alignItems: "center",
        }}
      >
        <span style={{ color: C.warn }}>▲</span>
        <div>
          Pass-rate dip starts at <strong style={{ color: C.text }}>v2.1.0 · model update</strong>. Bisect it in the Eval
          Matrix across the two versions spanning the dip.
        </div>
      </div>
    </div>
  );
}

export function AgentDetail() {
  const { state, dispatch } = useStore();
  const a = ALL_AGENTS.find((x) => x.id === state.agentDetail);
  if (!a) return null;
  const drift = a.id === "rev-analytics";
  return (
    <div>
      <button
        type="button"
        onClick={() => dispatch({ type: "closeAgentDetail" })}
        style={{ background: "none", border: "none", color: C.muted, fontSize: 13, cursor: "pointer", marginBottom: 14, padding: 0 }}
      >
        ← Fleet
      </button>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 22 }}>
        <Dot color={healthColor(a.health)} size={10} />
        <h1 style={{ fontSize: 24, fontWeight: 400, margin: 0, fontFamily: C.mono }}>{a.id}</h1>
        <Chip color={C.text2}>{a.ch}</Chip>
        <span style={{ marginLeft: "auto", fontSize: 13, color: C.muted }}>{a.runs + " runs today · " + a.cost}</span>
      </div>
      {drift ? (
        <DriftPanel />
      ) : (
        <Card>
          <div style={{ color: C.text2, fontSize: 14 }}>
            {"Healthy. Eval score " + a.score + "% · no drift detected in the last 30 days."}
          </div>
        </Card>
      )}
    </div>
  );
}
