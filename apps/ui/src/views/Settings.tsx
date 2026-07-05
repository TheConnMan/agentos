import { C } from "../tokens";
import { Card, SectionTitle, Button, Dot, CopyButton } from "../primitives";
import { hoverBg } from "../lib/style";
import { useStore } from "../state/store";

const MODELS: [string, string][] = [
  ["claude-sonnet-4.5", "Anthropic"],
  ["claude-haiku-4.5", "Anthropic"],
  ["gpt-4o", "OpenRouter"],
  ["llama-3.3-70b", "OpenRouter"],
];

const PROVIDERS = [
  { id: "anthropic", name: "Anthropic", secret: "sk-ant-api03-7Kd92MvQ1xLpZ0aB3nR8yT" },
  { id: "openrouter", name: "OpenRouter", secret: "sk-or-v1-a4f2c91b7e02d1c90ffb2d02a" },
];

function Field({ label, val, mono }: { label: string; val: string; mono?: boolean }) {
  return (
    <div style={{ padding: "14px 0", borderBottom: "1px solid " + C.border }}>
      <div style={{ fontSize: 13, fontWeight: 500, marginBottom: 6 }}>{label}</div>
      <input
        defaultValue={val}
        style={{
          width: "100%",
          maxWidth: 360,
          background: C.input,
          border: "1px solid " + C.borderStrong,
          borderRadius: 7,
          padding: "8px 10px",
          color: C.text,
          fontFamily: mono ? C.mono : "inherit",
          fontSize: 13,
        }}
      />
    </div>
  );
}

export function Settings() {
  const { state, dispatch } = useStore();
  const dm = state.defaultModel;
  const rev = state.provReveal;
  return (
    <div>
      <SectionTitle title="Settings" />
      <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 20, maxWidth: 640 }}>
        <Card>
          <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 6 }}>Project</div>
          <Field label="Project name" val="acme-corp" />
          <Field label="Default channel" val="#revenue-ops" mono />
          <Field label="Region" val="us-east-1 · hosted" mono />
          <div style={{ paddingTop: 16 }}>
            <Button label="Save changes" variant="primary" onClick={() => dispatch({ type: "toast", message: "Settings saved" })} />
          </div>
        </Card>

        <Card>
          <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 4 }}>Default model</div>
          <div style={{ fontSize: 12.5, color: C.muted, marginBottom: 14 }}>
            Agents can override per skill.md. The eval Matrix compares models side by side before you switch.
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {MODELS.map((m) => {
              const on = dm === m[0];
              return (
                <button
                  key={m[0]}
                  type="button"
                  onClick={() => dispatch({ type: "setDefaultModel", model: m[0] })}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    padding: "8px 12px",
                    borderRadius: 8,
                    cursor: "pointer",
                    background: on ? "rgba(62,207,142,.08)" : C.input,
                    border: "1px solid " + (on ? C.brand : C.border),
                    color: C.text,
                  }}
                  {...(on ? {} : hoverBg(C.input, C.hover))}
                >
                  <span
                    style={{
                      width: 13,
                      height: 13,
                      borderRadius: "50%",
                      border: "1.5px solid " + (on ? C.brand : C.borderMax),
                      background: on ? C.brand : "transparent",
                      display: "inline-block",
                    }}
                  />
                  <span style={{ fontFamily: C.mono, fontSize: 13 }}>{m[0]}</span>
                  <span style={{ fontSize: 11, color: C.muted }}>{m[1]}</span>
                </button>
              );
            })}
          </div>
        </Card>

        <Card>
          <div style={{ display: "flex", alignItems: "center", marginBottom: 6 }}>
            <div style={{ fontSize: 14, fontWeight: 500 }}>Provider keys</div>
            <div style={{ marginLeft: "auto" }}>
              <Button label="Add provider" size="sm" icon="+" onClick={() => dispatch({ type: "toast", message: "Add provider key" })} />
            </div>
          </div>
          <div style={{ fontSize: 12.5, color: C.muted, marginBottom: 6 }}>
            Bring your own keys. OpenRouter unlocks non-Anthropic models for the eval Matrix and model arbitrage.
          </div>
          {PROVIDERS.map((p, i) => (
            <div
              key={p.id}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 12,
                padding: "13px 0",
                borderTop: i ? "1px solid " + C.border : "none",
              }}
            >
              <div style={{ width: 130, display: "flex", alignItems: "center", gap: 8 }}>
                <Dot color={C.success} size={7} />
                <span style={{ fontSize: 13.5, fontWeight: 500 }}>{p.name}</span>
              </div>
              <span
                style={{
                  flex: 1,
                  fontFamily: C.mono,
                  fontSize: 12.5,
                  color: rev === p.id ? C.text : C.muted,
                  letterSpacing: rev === p.id ? 0 : 0.5,
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                {rev === p.id ? p.secret : p.secret.slice(0, 10) + "•".repeat(16)}
              </span>
              <Button
                label={rev === p.id ? "Hide" : "Reveal"}
                size="sm"
                onClick={() => dispatch({ type: "revealProvider", id: rev === p.id ? null : p.id })}
              />
              <CopyButton value={p.secret} />
            </div>
          ))}
        </Card>
      </div>
    </div>
  );
}
