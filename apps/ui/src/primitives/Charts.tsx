// SVG chart primitives ported from the canon: spark() (inline trend line) and
// areaChart() (filled area with a stroke). Both are pure and deterministic.

export function Sparkline({ data, color }: { data: number[]; color: string }) {
  const w = 68;
  const ht = 22;
  const mn = Math.min(...data);
  const mx = Math.max(...data);
  const rng = mx - mn || 1;
  const pts = data
    .map((v, i) => `${(i / (data.length - 1)) * w},${ht - ((v - mn) / rng) * (ht - 4) - 2}`)
    .join(" ");
  return (
    <svg width={w} height={ht} style={{ display: "block" }}>
      <polyline
        points={pts}
        fill="none"
        stroke={color}
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function AreaChart({ data, color, height = 60 }: { data: number[]; color: string; height?: number }) {
  const w = 300;
  const mn = Math.min(...data);
  const mx = Math.max(...data);
  const rng = mx - mn || 1;
  const xs = (i: number) => (i / (data.length - 1)) * w;
  const ys = (v: number) => height - 3 - ((v - mn) / rng) * (height - 9);
  const line = data.map((v, i) => xs(i).toFixed(1) + "," + ys(v).toFixed(1)).join(" ");
  return (
    <svg
      viewBox={`0 0 ${w} ${height}`}
      preserveAspectRatio="none"
      style={{ width: "100%", height, display: "block" }}
    >
      <polygon points={`0,${height} ${line} ${w},${height}`} fill={color} opacity={0.12} />
      <polyline points={line} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" />
    </svg>
  );
}
