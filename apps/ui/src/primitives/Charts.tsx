// SVG chart primitives ported from the canon: spark() (inline trend line) and
// areaChart() (filled area with a stroke). Both are pure and deterministic.
//
// Both guard the single-point and empty cases: dividing x by (length - 1) is
// NaN for one point and undefined for zero, so a one-point series (e.g. a weekly
// granularity over a short window) would otherwise break the SVG. One point
// renders as a flat line across the width; zero points render nothing.

export function Sparkline({ data, color }: { data: number[]; color: string }) {
  const w = 68;
  const ht = 22;
  if (data.length === 0) return <svg width={w} height={ht} style={{ display: "block" }} />;
  const mn = Math.min(...data);
  const mx = Math.max(...data);
  const rng = mx - mn || 1;
  const y = (v: number) => ht - ((v - mn) / rng) * (ht - 4) - 2;
  const pts =
    data.length === 1
      ? `0,${y(data[0]).toFixed(2)} ${w},${y(data[0]).toFixed(2)}`
      : data.map((v, i) => `${((i / (data.length - 1)) * w).toFixed(2)},${y(v).toFixed(2)}`).join(" ");
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
  if (data.length === 0) {
    return <svg viewBox={`0 0 ${w} ${height}`} preserveAspectRatio="none" style={{ width: "100%", height, display: "block" }} />;
  }
  const mn = Math.min(...data);
  const mx = Math.max(...data);
  const rng = mx - mn || 1;
  const ys = (v: number) => height - 3 - ((v - mn) / rng) * (height - 9);
  const coords =
    data.length === 1
      ? [
          [0, ys(data[0])],
          [w, ys(data[0])],
        ]
      : data.map((v, i): [number, number] => [(i / (data.length - 1)) * w, ys(v)]);
  const line = coords.map(([x, y]) => x.toFixed(1) + "," + y.toFixed(1)).join(" ");
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
