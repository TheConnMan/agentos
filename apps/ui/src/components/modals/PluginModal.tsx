import { C } from "../../tokens";
import { Button, Dot } from "../../primitives";
import { useStore } from "../../state/store";

const MANIFEST: [string, string][] = [
  ["3 skills", "triage-incident · summarize-alerts · page-oncall"],
  ["2 MCP servers", "datadog · pagerduty"],
  ["permissions", "read incidents · create pages · post to #incidents"],
];

// Install-plugin modal: drag-drop dropzone → manifest preview → Install. Ported
// from pluginModal(); Install promotes to level 5 and adds the sre-triage agent.
export function PluginModal() {
  const { state, dispatch } = useStore();
  const uploaded = state.pluginUploaded;
  return (
    <div style={{ width: 460, background: C.card, border: "1px solid " + C.borderStrong, borderRadius: 16, overflow: "hidden" }}>
      <div style={{ padding: "18px 24px", borderBottom: "1px solid " + C.border, display: "flex", alignItems: "center" }}>
        <h2 style={{ fontSize: 18, fontWeight: 500, margin: 0 }}>Install plugin</h2>
        <button
          type="button"
          onClick={() => dispatch({ type: "closeModal" })}
          style={{ marginLeft: "auto", background: "none", border: "none", color: C.muted, fontSize: 18, cursor: "pointer" }}
        >
          ✕
        </button>
      </div>
      <div style={{ padding: "22px 24px" }}>
        {!uploaded ? (
          <button
            type="button"
            onClick={() => dispatch({ type: "pluginUpload" })}
            style={{
              width: "100%",
              border: "1.5px dashed " + C.borderMax,
              borderRadius: 12,
              background: C.input,
              padding: "34px 20px",
              cursor: "pointer",
              color: C.text2,
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              gap: 10,
            }}
          >
            <div style={{ fontSize: 26, color: C.muted }}>⬒</div>
            <div style={{ fontSize: 13.5, color: C.text }}>Drop a plugin bundle here</div>
            <div style={{ fontSize: 12, color: C.muted }}>.zip or a git URL</div>
          </button>
        ) : (
          <div>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
                padding: "10px 12px",
                background: C.input,
                border: "1px solid " + C.border,
                borderRadius: 9,
                marginBottom: 16,
              }}
            >
              <span style={{ fontFamily: C.mono, fontSize: 16 }}>▣</span>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 13, fontFamily: C.mono }}>sre-triage-plugin.zip</div>
                <div style={{ fontSize: 11, color: C.muted }}>42 KB · verified</div>
              </div>
              <Dot color={C.brand} size={7} />
            </div>
            <div style={{ fontSize: 12, color: C.muted, marginBottom: 8, letterSpacing: 0.3 }}>MANIFEST</div>
            {MANIFEST.map((r, i) => (
              <div
                key={i}
                style={{
                  display: "flex",
                  gap: 12,
                  padding: "7px 0",
                  borderTop: i ? "1px solid " + C.border : "none",
                  fontSize: 13,
                }}
              >
                <span style={{ width: 110, color: C.text, fontWeight: 500 }}>{r[0]}</span>
                <span style={{ color: C.muted, fontFamily: C.mono, fontSize: 12 }}>{r[1]}</span>
              </div>
            ))}
          </div>
        )}
      </div>
      <div style={{ padding: "14px 24px", borderTop: "1px solid " + C.border, display: "flex", gap: 10, justifyContent: "flex-end" }}>
        <Button label="Cancel" variant="ghost" onClick={() => dispatch({ type: "closeModal" })} />
        <Button label="Install" variant="primary" disabled={!uploaded} onClick={() => dispatch({ type: "installPlugin" })} />
      </div>
    </div>
  );
}
