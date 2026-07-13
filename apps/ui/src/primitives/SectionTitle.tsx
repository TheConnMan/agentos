import type { ReactNode } from "react";
import { C } from "../tokens";

// View heading: 26px/400 title with an optional muted subtitle. Ported from
// sectionTitle(). Headings are deliberately not bold-heavy per the design.
// `right` renders a trailing control (e.g. a CliHint) on the title row.
export function SectionTitle({ title, sub, right }: { title: string; sub?: string; right?: ReactNode }) {
  return (
    <div style={{ marginBottom: 20 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <h1 style={{ fontSize: 26, fontWeight: 400, color: C.text, margin: "0 0 4px" }}>{title}</h1>
        {right ? <div style={{ marginLeft: "auto" }}>{right}</div> : null}
      </div>
      {sub ? <p style={{ fontSize: 14, color: C.muted, margin: 0 }}>{sub}</p> : null}
    </div>
  );
}
