import type { ReactNode } from "react";
import { C } from "../tokens";
import { hoverBg } from "../lib/style";

export interface TableRow {
  key: string;
  cells: ReactNode[];
  onClick?: () => void;
  /** Left accent rail color (e.g. a failed trace), optional. */
  accent?: string;
}

// Grid-based table used by the fleet, versions, traces, and cost views. The
// design renders tables as CSS grids (not <table>) so column widths follow a
// gridTemplateColumns string; this primitive centralizes that pattern.
export function Table({
  columns,
  headers,
  rows,
}: {
  columns: string;
  headers: string[];
  rows: TableRow[];
}) {
  return (
    <div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: columns,
          gap: 12,
          padding: "0 0 12px",
          fontSize: 12,
          color: C.muted,
          borderBottom: "1px solid " + C.border,
        }}
      >
        {headers.map((hdr, i) => (
          <div key={i}>{hdr}</div>
        ))}
      </div>
      {rows.map((r) => {
        const clickable = !!r.onClick;
        const base = {
          display: "grid",
          gridTemplateColumns: columns,
          gap: 12,
          padding: "13px 0" + (r.accent ? " 13px 12px" : ""),
          alignItems: "center",
          borderBottom: "1px solid " + C.border,
          background: "transparent",
          width: "100%",
          textAlign: "left" as const,
          color: C.text,
          fontSize: 13.5,
          borderLeft: "2px solid " + (r.accent ?? "transparent"),
        };
        if (!clickable) {
          return (
            <div key={r.key} style={base}>
              {r.cells.map((c, i) => (
                <div key={i} style={{ minWidth: 0 }}>
                  {c}
                </div>
              ))}
            </div>
          );
        }
        return (
          <button
            key={r.key}
            type="button"
            onClick={r.onClick}
            style={{ ...base, border: "none", cursor: "pointer" }}
            {...hoverBg("transparent", C.hover)}
          >
            {r.cells.map((c, i) => (
              <div key={i} style={{ minWidth: 0 }}>
                {c}
              </div>
            ))}
          </button>
        );
      })}
    </div>
  );
}
