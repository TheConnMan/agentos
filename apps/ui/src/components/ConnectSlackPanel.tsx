import type { ReactNode } from "react";
import { C } from "../tokens";
import { Card, CopyButton, Chip } from "../primitives";

// Honest Slack connection guidance (replaces the fixture OAuth-grant modal in
// wired mode). There is no real one-click OAuth yet: connecting Slack is an
// operator step (create the app from the committed manifest, drop the two tokens
// into the deploy secret, invite the bot). This panel tells the truth about that
// instead of a fake Allow button.
const MANIFEST_PATH = "apps/dispatcher/slack-app-manifest.yaml";

const STEPS: { n: number; title: string; body: ReactNode }[] = [
  {
    n: 1,
    title: "Create the Slack app from the manifest",
    body: (
      <>
        In Slack, create a new app "from an app manifest" and paste{" "}
        <span style={{ fontFamily: C.mono, color: C.text2 }}>{MANIFEST_PATH}</span> from this repo.
      </>
    ),
  },
  {
    n: 2,
    title: "Add the two tokens to your deployment",
    body: (
      <>
        Copy the Bot token (<span style={{ fontFamily: C.mono }}>xoxb-…</span>) and App-level token (
        <span style={{ fontFamily: C.mono }}>xapp-…</span>) into the dispatcher's secret/env
        (<span style={{ fontFamily: C.mono }}>SLACK_BOT_TOKEN</span>,{" "}
        <span style={{ fontFamily: C.mono }}>SLACK_APP_TOKEN</span>).
      </>
    ),
  },
  {
    n: 3,
    title: "Invite the bot and note the channel ID",
    body: (
      <>
        In the channel you want the agent to serve, run{" "}
        <span style={{ fontFamily: C.mono, color: C.text2 }}>/invite @your-bot</span>. Then grab that channel's ID (click
        the channel name → the ID is at the bottom of Channel details) — you give the agent that ID when you create it,
        because mentions are matched on the ID, not the name.
      </>
    ),
  },
];

export function ConnectSlackPanel() {
  return (
    <Card>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
        <div style={{ fontSize: 15, fontWeight: 500 }}>Connect Slack</div>
        <Chip color={C.mutedStatus} border={C.border}>
          operator setup
        </Chip>
      </div>
      <div style={{ fontSize: 13, color: C.muted, marginBottom: 16, maxWidth: 620, lineHeight: 1.5 }}>
        Curie talks to Slack through a Slack app you own. Three one-time steps wire it up; after that every agent you
        deploy is reachable in the channels you invite it to.
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        {STEPS.map((s) => (
          <div key={s.n} style={{ display: "flex", gap: 12 }}>
            <div
              style={{
                width: 24,
                height: 24,
                borderRadius: "50%",
                border: "1px solid " + C.borderStrong,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 12,
                fontFamily: C.mono,
                color: C.text2,
                flexShrink: 0,
              }}
            >
              {s.n}
            </div>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 13.5, fontWeight: 500, marginBottom: 2 }}>{s.title}</div>
              <div style={{ fontSize: 13, color: C.text2, lineHeight: 1.5 }}>{s.body}</div>
            </div>
          </div>
        ))}
      </div>
      <div
        style={{
          marginTop: 16,
          paddingTop: 14,
          borderTop: "1px solid " + C.border,
          display: "flex",
          alignItems: "center",
          gap: 10,
          flexWrap: "wrap",
        }}
      >
        <span style={{ fontSize: 12.5, color: C.muted }}>Manifest path</span>
        <span style={{ fontFamily: C.mono, fontSize: 12.5, color: C.text2 }}>{MANIFEST_PATH}</span>
        <CopyButton value={MANIFEST_PATH} />
        <span style={{ marginLeft: "auto", fontSize: 12.5, color: C.brand }} data-testid="slack-test-hint">
          How to test: mention the bot in your channel.
        </span>
      </div>
    </Card>
  );
}
