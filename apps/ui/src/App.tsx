import { C } from "./tokens";
import { useStore } from "./state/store";
import { Sidebar } from "./components/Sidebar";
import { Topbar, DevBanner } from "./components/Topbar";
import { ModalHost } from "./components/ModalHost";
import { Confetti } from "./components/Confetti";
import { Toast } from "./primitives";

import { Observability } from "./views/Observability";

import { WiredOverview } from "./views/wired/WiredOverview";
import { WiredAgents } from "./views/wired/WiredAgents";
import { WiredAgentDetail } from "./views/wired/WiredAgentDetail";
import { WiredConnections, WiredSettings } from "./views/wired/WiredStubs";
import { WiredEvals } from "./views/wired/WiredEvals";
import { WiredVersions } from "./views/wired/WiredVersions";

// The console is always backed by the live API. Each nav renders its
// backend-driven view; views that are not wired yet render an honest
// "Coming Soon" stub rather than demo data.
function Main() {
  const { state } = useStore();
  if (state.agentDetail) return <WiredAgentDetail />;
  switch (state.nav) {
    case "overview":
      return <WiredOverview />;
    case "agents":
      return <WiredAgents />;
    case "evals":
      return <WiredEvals />;
    case "observability":
      return <Observability />;
    case "versions":
      return <WiredVersions />;
    case "connections":
      return <WiredConnections />;
    case "settings":
      return <WiredSettings />;
    default:
      return <WiredOverview />;
  }
}

export function App() {
  const { envDev } = useStore();
  return (
    <div style={{ fontFamily: C.sans, color: C.text, minHeight: "100vh", background: C.page }}>
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
    </div>
  );
}
