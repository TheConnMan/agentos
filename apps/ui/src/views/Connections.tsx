import type { ReactNode } from "react";
import { C } from "../tokens";
import { Card, SectionTitle, Chip, Dot, Button, CopyButton, EmptyState } from "../primitives";
import { useStore } from "../state/store";

function ConnRow({
  icon,
  iconBg,
  title,
  sub,
  extra,
}: {
  icon: string;
  iconBg: string;
  title: string;
  sub: ReactNode;
  extra: ReactNode;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 14, padding: "16px 18px", borderBottom: "1px solid " + C.border }}>
      <div
        style={{
          width: 34,
          height: 34,
          borderRadius: 8,
          background: iconBg,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: 16,
          flexShrink: 0,
        }}
      >
        {icon}
      </div>
      <div style={{ flex: 1 }}>
        <div style={{ fontSize: 14, fontWeight: 500, display: "flex", alignItems: "center", gap: 8 }}>
          <Dot color={C.success} size={7} />
          {title}
        </div>
        <div style={{ fontSize: 12.5, color: C.muted, marginTop: 2, fontFamily: C.mono }}>{sub}</div>
      </div>
      {extra}
    </div>
  );
}

export function Connections() {
  const { state, dispatch, slackOn, ghOn } = useStore();
  if (state.level < 2) {
    return (
      <div>
        <SectionTitle title="Connections" />
        <EmptyState
          title="No connections yet"
          sub="Connect Slack to give your agents a home, then GitHub for CI evals."
          ctaLabel="Connect Slack"
          onCta={() => dispatch({ type: "openModal", modal: "slack-oauth" })}
          showDemo={false}
        />
      </div>
    );
  }
  return (
    <div>
      <SectionTitle title="Connections" />
      <Card style={{ padding: 20 }}>
        <div style={{ margin: -20 }}>
          <ConnRow
            icon="#"
            iconBg="#3b1d3a"
            title="Slack · acme-corp.slack.com"
            sub={
              <span>
                workspace <span style={{ color: C.text2 }}>T04AC8M2X</span>
                {"  ·  Connected 2 min ago"}
              </span>
            }
            extra={
              <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                <CopyButton label="T04AC8M2X" value="T04AC8M2X" />
              </div>
            }
          />
          {ghOn ? (
            <ConnRow
              icon="⑂"
              iconBg="#1c2a1e"
              title="GitHub · acme/agentos-agents"
              sub={<span>main → @agentos · dev → @agentos-dev  ·  Connected 1 day ago</span>}
              extra={
                <Chip color={C.brand} border="rgba(62,207,142,.4)">
                  CI evals on
                </Chip>
              }
            />
          ) : null}
        </div>
      </Card>
      {slackOn ? (
        <div style={{ marginTop: 20 }}>
          <Card>
            <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 4 }}>Bot token</div>
            <div style={{ fontSize: 12.5, color: C.muted, marginBottom: 14 }}>Used by the CLI and CI. Treat it like a password.</div>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
                background: C.input,
                border: "1px solid " + C.border,
                borderRadius: 9,
                padding: "10px 12px",
              }}
            >
              <span
                style={{
                  flex: 1,
                  fontFamily: C.mono,
                  fontSize: 13,
                  color: state.tokenRevealed ? C.text : C.muted,
                  letterSpacing: state.tokenRevealed ? 0 : 1,
                }}
              >
                {state.tokenRevealed ? "rly_secret_7Kd92MvQ1xLpZ0aB3nR8yT" : "rly_secret_" + "•".repeat(22)}
              </span>
              <Button
                label={state.tokenRevealed ? "Hide" : "Reveal"}
                size="sm"
                onClick={() => dispatch({ type: "revealToken", value: !state.tokenRevealed })}
              />
              <CopyButton label="Copy" value="rly_secret_7Kd92MvQ1xLpZ0aB3nR8yT" />
            </div>
          </Card>
        </div>
      ) : null}
    </div>
  );
}
