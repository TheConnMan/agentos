import { C } from "../../tokens";
import { Card, Chip, Dot, Button } from "../../primitives";
import { hoverBg } from "../../lib/style";
import { useStore } from "../../state/store";
import { tracesForLevel, traceSpans } from "../../fixtures";

export function TracesList() {
  const { state, dispatch, envDev } = useStore();
  const traces = tracesForLevel(state.level);
  const grid = "1.6fr 1fr .6fr .6fr .6fr .8fr";
  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
        <div
          style={{
            flex: 1,
            display: "flex",
            alignItems: "center",
            gap: 8,
            background: C.input,
            border: "1px solid " + C.border,
            borderRadius: 8,
            padding: "7px 11px",
            fontFamily: C.mono,
            fontSize: 12.5,
            color: C.text2,
          }}
        >
          <span style={{ color: C.muted }}>trace</span>
          <span style={{ color: C.text }}>{'{ agent="deal-desk", env="' + (envDev ? "dev" : "prod") + '" }'}</span>
          <span style={{ marginLeft: "auto", color: C.muted }}>{traces.length + " spans"}</span>
        </div>
        <Chip color={C.mutedStatus}>OTel</Chip>
      </div>
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
          {["Message", "Trace", "Tools", "Latency", "Cost", "When"].map((c) => (
            <div key={c}>{c}</div>
          ))}
        </div>
        {traces.map((t) => {
          const bad = t.status === "fail";
          return (
            <button
              key={t.id}
              type="button"
              onClick={() => dispatch({ type: "openTrace", id: t.id })}
              style={{
                display: "grid",
                gridTemplateColumns: grid,
                gap: 12,
                padding: "13px 0 13px 12px",
                alignItems: "center",
                borderBottom: "1px solid " + C.border,
                background: "transparent",
                border: "none",
                width: "100%",
                textAlign: "left",
                cursor: "pointer",
                color: C.text,
                fontSize: 13,
                borderLeft: "2px solid " + (bad ? C.destructive : "transparent"),
              }}
              {...hoverBg("transparent", C.hover)}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 8, overflow: "hidden" }}>
                <Dot color={bad ? C.destructive : C.success} size={7} />
                <span style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", color: C.text2 }}>{t.msg}</span>
              </div>
              <span style={{ fontFamily: C.mono, fontSize: 12.5, color: bad ? C.destructive : C.link }}>{t.id}</span>
              <span style={{ fontFamily: C.mono, color: C.text2 }}>{t.tools}</span>
              <span style={{ fontFamily: C.mono, color: C.text2 }}>{t.dur}</span>
              <span style={{ fontFamily: C.mono, color: C.text2 }}>{t.cost}</span>
              <span style={{ color: C.muted, fontSize: 12.5 }}>{t.when}</span>
            </button>
          );
        })}
      </Card>
    </div>
  );
}

export function TraceDetail() {
  const { state, dispatch } = useStore();
  const traces = tracesForLevel(state.level);
  const t = traces.find((x) => x.id === state.traceOpen) ?? traces[0];
  const bad = t.status === "fail";
  const spans = traceSpans(t);
  return (
    <div>
      <button
        type="button"
        onClick={() => dispatch({ type: "closeTrace" })}
        style={{ background: "none", border: "none", color: C.muted, fontSize: 13, cursor: "pointer", marginBottom: 14, padding: 0 }}
      >
        ← Traces
      </button>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 20 }}>
        <h1 style={{ fontSize: 22, fontWeight: 400, margin: 0, fontFamily: C.mono }}>{t.id}</h1>
        <Chip
          color={bad ? C.destructive : C.success}
          border={bad ? "rgba(229,77,46,.4)" : "rgba(46,160,67,.4)"}
          pre={<Dot color={bad ? C.destructive : C.success} size={6} />}
        >
          {bad ? "failed" : "ok"}
        </Chip>
        <span style={{ marginLeft: "auto", fontSize: 12.5, color: C.muted, fontFamily: C.mono }}>
          {t.dur + " · " + t.tools + " tool calls · " + t.tokens + " tokens · " + t.cost}
        </span>
      </div>
      <Card>
        {spans.map((s, i) => {
          const last = i === spans.length - 1;
          const color = s.bad ? C.destructive : s.kind === "response" ? C.success : s.kind === "message" ? C.mutedStatus : C.text2;
          return (
            <div key={i} style={{ display: "flex", gap: 14, paddingBottom: last ? 0 : 18 }}>
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center" }}>
                <Dot color={color} size={10} />
                {!last ? <div style={{ width: 1.5, flex: 1, background: C.border, minHeight: 28, marginTop: 3 }} /> : null}
              </div>
              <div style={{ flex: 1, paddingBottom: 6 }}>
                <div style={{ display: "flex", gap: 10, alignItems: "baseline" }}>
                  <span style={{ fontSize: 13.5, fontWeight: 500 }}>{s.label}</span>
                  <span style={{ fontSize: 11, color: C.muted, fontFamily: C.mono }}>{s.offset}</span>
                </div>
                <div
                  style={{
                    color: s.bad ? C.destructive : C.text2,
                    marginTop: 3,
                    fontFamily: i > 0 ? C.mono : "inherit",
                    fontSize: i > 0 ? 12.5 : 13.5,
                  }}
                >
                  {s.detail}
                </div>
              </div>
            </div>
          );
        })}
      </Card>
      {bad ? (
        <div
          style={{
            marginTop: 18,
            padding: "18px 20px",
            background: "rgba(229,77,46,.06)",
            border: "1px solid rgba(229,77,46,.3)",
            borderRadius: 12,
            display: "flex",
            alignItems: "center",
            gap: 16,
          }}
        >
          <div>
            <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 3 }}>Turn this failure into a guardrail</div>
            <div style={{ fontSize: 13, color: C.text2 }}>Add this trace as an eval case so the regression can never ship again.</div>
          </div>
          {state.promoteForm ? (
            <div style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" }}>
              <input
                defaultValue="approver-from-policy-source"
                style={{
                  background: C.input,
                  border: "1px solid " + C.borderStrong,
                  borderRadius: 7,
                  padding: "7px 10px",
                  color: C.text,
                  fontFamily: C.mono,
                  fontSize: 12.5,
                  width: 250,
                }}
              />
              <Button label="Save case" variant="primary" size="sm" onClick={() => dispatch({ type: "promoteEval" })} />
            </div>
          ) : (
            <div style={{ marginLeft: "auto" }}>
              <Button label="Add as eval case" variant="primary" onClick={() => dispatch({ type: "promoteFormOpen" })} />
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
