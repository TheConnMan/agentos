import { SectionTitle, Tabs } from "../primitives";
import { useStore } from "../state/store";
import type { ObsTab } from "../state/types";
import { RealTracesList, RealTraceDetail } from "./obs/RealTraces";
import { RealMetrics } from "./obs/RealMetrics";
import { RealLogs } from "./obs/RealLogs";
import { RealCost } from "./obs/RealCost";
import { RealMemory } from "./obs/RealMemory";
import { RealApprovals } from "./obs/RealApprovals";
import { WiredUsage } from "./wired/WiredStubs";

const TABS: [ObsTab, string][] = [
  ["traces", "Traces"],
  ["metrics", "Metrics"],
  ["logs", "Logs"],
  ["approvals", "Approvals"],
  ["memory", "Memory"],
  ["usage", "Usage"],
  ["cost", "Cost"],
];

export function Observability() {
  const { state, dispatch } = useStore();

  const tab = state.obsTab;
  let content;
  switch (tab) {
    case "traces":
      content = state.traceOpen ? <RealTraceDetail /> : <RealTracesList />;
      break;
    case "metrics":
      content = <RealMetrics />;
      break;
    case "logs":
      content = <RealLogs />;
      break;
    case "memory":
      content = <RealMemory />;
      break;
    case "usage":
      content = <WiredUsage />;
      break;
    case "cost":
      content = <RealCost />;
      break;
    case "approvals":
      content = <RealApprovals />;
      break;
  }

  return (
    <div>
      <SectionTitle
        title="Observability"
        sub="OpenTelemetry traces, Prometheus-style metrics, and Loki-style logs — on by default."
      />
      <Tabs
        tabs={TABS}
        active={tab}
        onSelect={(id) => dispatch({ type: "setObsTab", tab: id })}
      />
      {content}
    </div>
  );
}
