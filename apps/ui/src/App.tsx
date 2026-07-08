import { C } from "./tokens";
import { useStore } from "./state/store";
import { isWired } from "./api/config";
import { Sidebar } from "./components/Sidebar";
import { Topbar, DevBanner } from "./components/Topbar";
import { ModalHost } from "./components/ModalHost";
import { Confetti } from "./components/Confetti";
import { StateSwitcher } from "./components/StateSwitcher";
import { Toast } from "./primitives";

import { Overview } from "./views/Overview";
import { Agents } from "./views/Agents";
import { Evals } from "./views/Evals";
import { Observability } from "./views/Observability";
import { Versions } from "./views/Versions";
import { Connections } from "./views/Connections";
import { Settings } from "./views/Settings";
import { AgentDetail } from "./views/AgentDetail";

import { WiredOverview } from "./views/wired/WiredOverview";
import { WiredAgents } from "./views/wired/WiredAgents";
import { WiredAgentDetail } from "./views/wired/WiredAgentDetail";
import { WiredEvals, WiredConnections, WiredSettings } from "./views/wired/WiredStubs";
import { WiredVersions } from "./views/wired/WiredVersions";

// Wired mode renders the backend-driven shell (real agents/onboarding); unwired
// renders the fixture demo (?state=N). Observability and Settings branch on
// isWired() internally, so they stay shared. The two worlds never mix.
function Main() {
  const { state } = useStore();
  const wired = isWired();
  if (state.agentDetail) return wired ? <WiredAgentDetail /> : <AgentDetail />;
  switch (state.nav) {
    case "overview":
      return wired ? <WiredOverview /> : <Overview />;
    case "agents":
      return wired ? <WiredAgents /> : <Agents />;
    case "evals":
      return wired ? <WiredEvals /> : <Evals />;
    case "observability":
      return <Observability />;
    case "versions":
      return wired ? <WiredVersions /> : <Versions />;
    case "connections":
      return wired ? <WiredConnections /> : <Connections />;
    case "settings":
      return wired ? <WiredSettings /> : <Settings />;
    default:
      return wired ? <WiredOverview /> : <Overview />;
  }
}

// Fixture mode serves a convincing demo on fake data. Without this banner a
// first-time viewer can't tell it apart from the live product, so it is always
// present when not wired, and links straight to the wired app.
function DemoBanner() {
  return (
    <div
      data-testid="demo-banner"
      style={{
        background: "rgba(62,207,142,.12)",
        borderBottom: "1px solid rgba(62,207,142,.35)",
        padding: "8px 36px",
        fontSize: 12.5,
        color: C.brand,
        fontFamily: C.mono,
        display: "flex",
        alignItems: "center",
        gap: 10,
      }}
    >
      <span style={{ fontWeight: 600 }}>Demo data</span>
      <span style={{ color: C.text2 }}>— not connected to a backend. Everything here is fixtures.</span>
      <a
        href="?api=1&state=1"
        data-testid="demo-banner-connect"
        style={{ marginLeft: "auto", color: C.link, textDecoration: "none", fontWeight: 500 }}
      >
        Connect to a backend →
      </a>
    </div>
  );
}

export function App() {
  const { envDev } = useStore();
  const wired = isWired();
  return (
    <div style={{ fontFamily: C.sans, color: C.text, minHeight: "100vh", background: C.page }}>
      {!wired ? <DemoBanner /> : null}
      <div style={{ display: "flex", minHeight: "100vh" }}>
        <Sidebar />
        <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
          <Topbar />
          {envDev ? <DevBanner /> : null}
          <div style={{ flex: 1, padding: "28px 36px", maxWidth: 1280, width: "100%", margin: "0 auto" }}>
            <Main />
          </div>
        </div>
      </div>
      <ModalHost />
      <Toast />
      <Confetti />
      <StateSwitcher />
    </div>
  );
}
