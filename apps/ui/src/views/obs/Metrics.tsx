import { C } from "../../tokens";
import { Card, AreaChart } from "../../primitives";
import { useStore } from "../../state/store";
import { METRIC_PANELS, REQUEST_RATE_SERIES } from "../../fixtures";
import type { MetricPanel } from "../../fixtures";
import type { MetricRange } from "../../state/types";

const RANGES: MetricRange[] = ["1h", "6h", "24h", "7d"];
const COLOR: Record<MetricPanel["color"], string> = {
  brand: C.brand,
  warn: C.warn,
  link: C.link,
  mutedStatus: C.mutedStatus,
};

function Panel({ p }: { p: MetricPanel }) {
  return (
    <Card style={{ padding: "14px 16px" }}>
      <div style={{ display: "flex", alignItems: "baseline", marginBottom: 2 }}>
        <div style={{ fontSize: 12.5, color: C.muted }}>{p.title}</div>
        <div style={{ marginLeft: "auto", fontFamily: C.mono, fontSize: 18, color: C.text }}>
          {p.value}
          <span style={{ fontSize: 11, color: C.muted, marginLeft: 3 }}>{p.unit}</span>
        </div>
      </div>
      <div style={{ marginTop: 8 }}>
        <AreaChart data={p.series} color={COLOR[p.color]} height={52} />
      </div>
    </Card>
  );
}

export function Metrics() {
  const { state, dispatch } = useStore();
  const range = state.metricRange;
  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16 }}>
        <div
          style={{
            flex: 1,
            display: "flex",
            alignItems: "center",
            gap: 8,
            background: C.input,
            border: "1px solid " + C.border,
            borderRadius: 8,
            padding: "8px 11px",
            fontFamily: C.mono,
            fontSize: 12.5,
          }}
        >
          <span style={{ color: C.brand }}>rate</span>
          <span style={{ color: C.text2 }}>(agentos_requests_total{'{agent="deal-desk"}'}[5m])</span>
        </div>
        <div style={{ display: "flex", background: C.card, border: "1px solid " + C.border, borderRadius: 8, padding: 2 }}>
          {RANGES.map((r) => (
            <button
              key={r}
              type="button"
              onClick={() => dispatch({ type: "setMetricRange", range: r })}
              style={{
                fontFamily: C.mono,
                fontSize: 12,
                padding: "4px 10px",
                borderRadius: 6,
                border: "none",
                cursor: "pointer",
                background: range === r ? C.hover : "transparent",
                color: range === r ? C.text : C.muted,
              }}
            >
              {r}
            </button>
          ))}
        </div>
      </div>
      <Card style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", alignItems: "baseline", marginBottom: 6 }}>
          <div style={{ fontSize: 13, color: C.text2 }}>Request rate</div>
          <div style={{ marginLeft: "auto", fontFamily: C.mono, fontSize: 22 }}>
            7.2<span style={{ fontSize: 12, color: C.muted, marginLeft: 4 }}>req/s</span>
          </div>
        </div>
        <AreaChart data={REQUEST_RATE_SERIES} color={C.brand} height={90} />
      </Card>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 14 }}>
        {METRIC_PANELS.map((p) => (
          <Panel key={p.title} p={p} />
        ))}
      </div>
    </div>
  );
}
