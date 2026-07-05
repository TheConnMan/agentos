import { C } from "../tokens";
import { Dot } from "./Dot";
import type { Health, TraceStatus, VersionState } from "../fixtures/types";

// Maps a semantic health/status to its token color, so fixtures stay free of
// presentation tokens and every view resolves color the same way.
export function healthColor(h: Health): string {
  return h === "green" ? C.success : h === "amber" ? C.warn : C.failure;
}

export function traceColor(s: TraceStatus): string {
  return s === "fail" ? C.destructive : C.success;
}

export function versionColor(s: VersionState): string {
  return s === "production" ? C.success : s === "preview" ? C.warn : C.failure;
}

// A labeled status indicator: dot + capitalized text. Used in the fleet table.
export function StatusDot({
  color,
  label,
  size = 8,
}: {
  color: string;
  label?: string;
  size?: number;
}) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 7 }}>
      <Dot color={color} size={size} />
      {label ? (
        <span style={{ fontSize: 12, color: C.muted, textTransform: "capitalize" }}>{label}</span>
      ) : null}
    </span>
  );
}
