import type { CSSProperties, MouseEvent } from "react";

// The design system swaps background on hover via inline JS handlers rather than
// CSS :hover (to keep every element self-contained). hoverBg reproduces the
// canon's `hb(bg, hbg)` helper: return mouse handlers that swap the background.
export function hoverBg(bg: string, hbg: string) {
  return {
    onMouseEnter: (e: MouseEvent<HTMLElement>) => {
      e.currentTarget.style.background = hbg;
    },
    onMouseLeave: (e: MouseEvent<HTMLElement>) => {
      e.currentTarget.style.background = bg;
    },
  };
}

// Merge helper that drops undefined entries, so callers can spread optional style.
export function sx(...parts: (CSSProperties | undefined | false)[]): CSSProperties {
  return Object.assign({}, ...parts.filter(Boolean));
}
