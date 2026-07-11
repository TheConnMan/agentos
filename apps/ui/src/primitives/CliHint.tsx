import { useState } from "react";
import { C } from "../tokens";
import { useStore } from "../state/store";

// CliHint: a resting `>_` glyph that morphs in place into a copy button on
// hover / keyboard-focus (cross-fades to `⧉`, accent color), previews the exact
// command in a tooltip, and copies on a single click (glyph flips to a green
// `✓` and fires the "Copied" toast). Composes the same clipboard + toast
// affordance as CopyButton, in a self-contained inline control.
//
// Keyboard-accessible (it is a real <button>, so Enter/Space activate it); on
// touch, a tap copies directly (there is no hover step to gate on). The
// morph is driven by hover/focus state and a transient "copied" flag, all
// CSS transitions so it degrades gracefully.

const COPIED_RESET_MS = 1200;

export function CliHint({ command, label }: { command: string; label?: string }) {
  const { dispatch } = useStore();
  const [active, setActive] = useState(false); // hover or keyboard focus
  const [copied, setCopied] = useState(false);

  function copy() {
    if (navigator.clipboard) {
      void navigator.clipboard.writeText(command).catch(() => {});
    }
    dispatch({ type: "toast", message: "Copied" });
    setCopied(true);
    window.setTimeout(() => setCopied(false), COPIED_RESET_MS);
  }

  // Resting `>_`; morphs to `⧉` on hover/focus; flips to `✓` right after copy.
  const glyph = copied ? "✓" : active ? "⧉" : ">_";
  const glyphColor = copied ? C.brand : active ? C.link : C.muted;

  return (
    <button
      type="button"
      onClick={copy}
      onMouseEnter={() => setActive(true)}
      onMouseLeave={() => setActive(false)}
      onFocus={() => setActive(true)}
      onBlur={() => setActive(false)}
      title={`$ ${command}`}
      aria-label={`Copy command: ${command}`}
      data-copied={copied ? "true" : "false"}
      style={{
        background: "transparent",
        border: "none",
        color: glyphColor,
        cursor: "pointer",
        fontFamily: C.mono,
        fontSize: 12,
        padding: "2px 4px",
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
      }}
    >
      <span
        aria-hidden="true"
        style={{
          color: glyphColor,
          width: "1.4em",
          textAlign: "center",
          transition: "color .15s ease",
        }}
      >
        {glyph}
      </span>
      {label ? <span>{label}</span> : null}
    </button>
  );
}
