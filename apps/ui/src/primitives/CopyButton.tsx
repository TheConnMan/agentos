import { C } from "../tokens";
import { useStore } from "../state/store";

// Copy affordance: an inline "⧉" glyph plus an optional label. Fires the
// "Copied" toast (canon copyBtn); the clipboard write is best-effort.
export function CopyButton({ label, value }: { label?: string; value?: string }) {
  const { dispatch } = useStore();
  return (
    <button
      type="button"
      onClick={() => {
        if (value && navigator.clipboard) void navigator.clipboard.writeText(value).catch(() => {});
        dispatch({ type: "toast", message: "Copied" });
      }}
      style={{
        background: "transparent",
        border: "none",
        color: C.muted,
        cursor: "pointer",
        fontFamily: C.mono,
        fontSize: 12,
        padding: "2px 4px",
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
      }}
    >
      <span style={{ color: C.muted }}>⧉</span>
      {label ? <span>{label}</span> : null}
    </button>
  );
}
