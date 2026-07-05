import { C } from "../../tokens";
import { useStore } from "../../state/store";
import { logsForLevel } from "../../fixtures";
import type { LogLevel } from "../../fixtures";

const LEVEL_COLOR: Record<LogLevel, string> = {
  info: C.mutedStatus,
  warn: C.warn,
  error: C.failure,
};

export function Logs() {
  const { ghOn } = useStore();
  const logs = logsForLevel(ghOn);
  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
        <div
          style={{
            flex: 1,
            display: "flex",
            alignItems: "center",
            gap: 6,
            background: C.input,
            border: "1px solid " + C.border,
            borderRadius: 8,
            padding: "8px 11px",
            fontFamily: C.mono,
            fontSize: 12.5,
          }}
        >
          <span style={{ color: C.text2 }}>{'{agent="deal-desk"} '}</span>
          <span style={{ color: C.brand }}>|= </span>
          <span style={{ color: C.text2 }}>""</span>
        </div>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 7,
            fontFamily: C.mono,
            fontSize: 12,
            color: C.brand,
            padding: "6px 12px",
            border: "1px solid rgba(62,207,142,.35)",
            borderRadius: 8,
          }}
        >
          <span style={{ width: 7, height: 7, borderRadius: "50%", background: C.brand, animation: "blink 1.4s step-end infinite" }} />
          Live tail
        </div>
      </div>
      <div
        style={{
          background: C.darkest,
          border: "1px solid " + C.border,
          borderRadius: 12,
          overflow: "hidden",
          fontFamily: C.mono,
          fontSize: 12.5,
        }}
      >
        {logs.map((l, i) => (
          <div
            key={i}
            style={{
              display: "flex",
              gap: 12,
              padding: "8px 14px",
              borderTop: i ? "1px solid " + C.border : "none",
              alignItems: "flex-start",
            }}
          >
            <span style={{ color: C.muted, whiteSpace: "nowrap" }}>{l.ts}</span>
            <span
              style={{
                color: LEVEL_COLOR[l.level],
                fontWeight: 600,
                width: 44,
                flexShrink: 0,
                textTransform: "uppercase",
                fontSize: 11,
              }}
            >
              {l.level}
            </span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <span style={{ color: l.level === "error" ? C.failure : C.text }}>{l.msg}</span>
              <span style={{ color: C.muted, marginLeft: 10 }}>
                {Object.entries(l.fields)
                  .map(([k, v]) => k + "=" + v)
                  .join("  ")}
              </span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
