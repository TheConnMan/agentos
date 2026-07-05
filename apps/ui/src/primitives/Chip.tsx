import type { ReactNode } from "react";
import { C } from "../tokens";

// Pill chip ported from the canon's chip(). `pre` renders a leading node
// (typically a Dot) inside the pill.
export function Chip({
  children,
  color,
  border,
  bg,
  pre,
}: {
  children: ReactNode;
  color?: string;
  border?: string;
  bg?: string;
  pre?: ReactNode;
}) {
  return (
    <span
      style={{
        fontFamily: C.mono,
        fontSize: 11,
        padding: "2px 8px",
        borderRadius: 20,
        border: "1px solid " + (border ?? C.border),
        background: bg ?? "transparent",
        color: color ?? C.text2,
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
      }}
    >
      {pre ?? null}
      {children}
    </span>
  );
}
