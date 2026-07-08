import type { ReactNode } from "react";
import { C } from "../tokens";

// Centered muted one-liner for inline loading/empty/error states inside a card
// or view. Padding and font size default to the common treatment; the two
// outlier call sites (a taller drill-in, a roomier traces list) override them.
export function Notice({
  children,
  padding = "30px 20px",
  fontSize = 13,
}: {
  children: ReactNode;
  padding?: string;
  fontSize?: number;
}) {
  return <div style={{ padding, textAlign: "center", color: C.muted, fontSize }}>{children}</div>;
}
