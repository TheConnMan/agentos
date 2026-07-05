import { C } from "../../tokens";
import { Card, SectionTitle, Chip } from "../../primitives";
import { ConnectSlackPanel } from "../../components/ConnectSlackPanel";

// Honest placeholders for the views not yet backend-driven, so wired mode never
// leaks fixture data (no fictional deal-desk / eval cases / version rows). They
// state plainly what is not wired yet rather than showing demo data.

function ComingSoon({ title, body }: { title: string; body: string }) {
  return (
    <Card>
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          textAlign: "center",
          padding: "56px 20px",
          gap: 12,
        }}
      >
        <div
          style={{
            width: 44,
            height: 44,
            borderRadius: 10,
            border: "1px solid " + C.borderStrong,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: C.muted,
            fontSize: 20,
          }}
        >
          ○
        </div>
        <div style={{ fontSize: 15, color: C.text }}>{title}</div>
        <div style={{ fontSize: 13, color: C.muted, maxWidth: 420 }}>{body}</div>
      </div>
    </Card>
  );
}

export function WiredEvals() {
  return (
    <div>
      <SectionTitle title="Evals" sub="Fixed test cases run against a version + model, on every PR." />
      <ComingSoon
        title="No eval runs yet"
        body="Eval suites and the version matrix light up here once the eval runner is connected. Nothing to show for a fresh workspace."
      />
    </div>
  );
}

export function WiredVersions() {
  return (
    <div>
      <SectionTitle title="Versions" sub="main → your prod bot · dev → your dev bot." />
      <ComingSoon
        title="No versions deployed yet"
        body="Deploy an agent or push to a connected git branch and its versions appear here. This workspace has none yet."
      />
    </div>
  );
}

export function WiredUsage() {
  return (
    <ComingSoon
      title="No usage analytics yet"
      body="Top users, intents, and override rates surface here once there is live traffic. Nothing to show for a fresh workspace."
    />
  );
}

export function WiredSettings() {
  return (
    <div>
      <SectionTitle title="Settings" />
      <ComingSoon
        title="Project settings are not wired yet"
        body="Project name, default model, and provider keys are managed outside the console for now. This view will bind to real settings in a later pass."
      />
    </div>
  );
}

export function WiredConnections() {
  return (
    <div>
      <SectionTitle title="Connections" />
      <div style={{ display: "flex", flexDirection: "column", gap: 16, maxWidth: 720 }}>
        <ConnectSlackPanel />
        <Card>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
            <div style={{ fontSize: 15, fontWeight: 500 }}>GitHub</div>
            <Chip color={C.mutedStatus} border={C.border}>
              optional
            </Chip>
          </div>
          <div style={{ fontSize: 13, color: C.muted, lineHeight: 1.5, maxWidth: 620 }}>
            Connect a GitHub repo to deploy agents via git-flow (main → prod bot, dev → dev bot) and run evals as PR
            checks. Not required to deploy from the browser.
          </div>
        </Card>
      </div>
    </div>
  );
}
