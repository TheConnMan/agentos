import { useMemo } from "react";
import { C } from "../tokens";

// The line-numbered mono skill.md editor, shared by the create-agent modal and
// the wired agent-detail surface so both edit skills in exactly one visual
// style. The parent controls sizing: by default it fills its flex parent; pass
// `height` for a fixed-height editor (the agent-detail file view).
export function SkillEditor({
  path,
  value,
  onChange,
  testId = "skill-editor",
  height,
}: {
  path: string;
  value: string;
  onChange: (next: string) => void;
  testId?: string;
  height?: number;
}) {
  const lineCount = useMemo(() => value.split("\n").length, [value]);
  return (
    <div style={{ flex: height ? undefined : 1, minWidth: 0, display: "flex", flexDirection: "column", minHeight: 0 }}>
      <div
        style={{
          padding: "8px 14px",
          borderBottom: "1px solid " + C.border,
          fontFamily: C.mono,
          fontSize: 12,
          color: C.muted,
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}
      >
        <span style={{ color: C.text2 }}>{path}</span>
      </div>
      <div
        style={{
          flex: height ? undefined : 1,
          height,
          overflow: "hidden",
          background: C.darkest,
          display: "flex",
          fontFamily: C.mono,
          fontSize: 12.5,
          lineHeight: 1.55,
          minHeight: 0,
        }}
      >
        <div
          style={{
            padding: "12px 8px",
            color: C.disabled,
            textAlign: "right",
            userSelect: "none",
            borderRight: "1px solid " + C.border,
            overflow: "hidden",
          }}
        >
          {Array.from({ length: lineCount }, (_, i) => (
            <div key={i}>{i + 1}</div>
          ))}
        </div>
        <textarea
          data-testid={testId}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          spellCheck={false}
          style={{
            margin: 0,
            padding: "12px 16px",
            color: C.text2,
            background: "transparent",
            border: "none",
            outline: "none",
            resize: "none",
            whiteSpace: "pre",
            flex: 1,
            fontFamily: C.mono,
            fontSize: 12.5,
            lineHeight: 1.55,
          }}
        />
      </div>
    </div>
  );
}
