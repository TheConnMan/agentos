import { C } from "./tokens";
import { useStore } from "./state/store";
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
import { Terminal } from "./views/Terminal";

function Main() {
  const { state } = useStore();
  if (state.terminal) return <Terminal />;
  if (state.agentDetail) return <AgentDetail />;
  switch (state.nav) {
    case "overview":
      return <Overview />;
    case "agents":
      return <Agents />;
    case "evals":
      return <Evals />;
    case "observability":
      return <Observability />;
    case "versions":
      return <Versions />;
    case "connections":
      return <Connections />;
    case "settings":
      return <Settings />;
    default:
      return <Overview />;
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
          <div style={{ flex: 1, padding: "28px 36px", maxWidth: 1280, width: "100%" }}>
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
