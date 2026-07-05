import { C } from "../tokens";

// View heading: 26px/400 title with an optional muted subtitle. Ported from
// sectionTitle(). Headings are deliberately not bold-heavy per the design.
export function SectionTitle({ title, sub }: { title: string; sub?: string }) {
  return (
    <div style={{ marginBottom: 20 }}>
      <h1 style={{ fontSize: 26, fontWeight: 400, color: C.text, margin: "0 0 4px" }}>{title}</h1>
      {sub ? <p style={{ fontSize: 14, color: C.muted, margin: 0 }}>{sub}</p> : null}
    </div>
  );
}
