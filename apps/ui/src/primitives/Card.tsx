import type { CSSProperties, ReactNode } from "react";
import { C, R } from "../tokens";

// Surface card: 1px border elevation, 14px radius. Ported from card().
export function Card({ children, style }: { children: ReactNode; style?: CSSProperties }) {
  return (
    <div
      style={{
        background: C.card,
        border: "1px solid " + C.border,
        borderRadius: R.card,
        padding: 20,
        ...style,
      }}
    >
      {children}
    </div>
  );
}
