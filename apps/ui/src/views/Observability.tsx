import { SectionTitle, EmptyState, Tabs } from "../primitives";
import { useStore } from "../state/store";
import { isWired } from "../api/config";
import type { ObsTab } from "../state/types";
import { TracesList, TraceDetail } from "./obs/Traces";
import { RealTracesList, RealTraceDetail } from "./obs/RealTraces";
import { Metrics } from "./obs/Metrics";
import { RealMetrics } from "./obs/RealMetrics";
import { Logs } from "./obs/Logs";
import { RealLogs } from "./obs/RealLogs";
import { MemoryStub } from "./obs/MemoryStub";
import { Usage } from "./obs/Usage";
import { Cost } from "./obs/Cost";

const TABS: [ObsTab, string][] = [
  ["traces", "Traces"],
  ["metrics", "Metrics"],
  ["logs", "Logs"],
  ["memory", "Memory"],
  ["usage", "Usage"],
  ["cost", "Cost"],
];

export function Observability() {
  const { state, dispatch } = useStore();

  if (state.level < 3) {
    return (
      <div>
        <SectionTitle title="Observability" />
        <EmptyState
          title="No telemetry yet"
          sub="Deploy an agent and traces, metrics, and logs turn on automatically — no instrumentation, no agents to install."
          ctaLabel="Create an agent"
          onCta={() => dispatch({ type: "openModal", modal: "new-agent" })}
        />
      </div>
    );
  }

  const tab = state.obsTab;
  let content;
  switch (tab) {
    case "traces":
      if (isWired()) {
        content = state.traceOpen ? <RealTraceDetail /> : <RealTracesList />;
      } else {
        content = state.traceOpen ? <TraceDetail /> : <TracesList />;
      }
      break;
    case "metrics":
      content = isWired() ? <RealMetrics /> : <Metrics />;
      break;
    case "logs":
      content = isWired() ? <RealLogs /> : <Logs />;
      break;
    case "memory":
      content = <MemoryStub />;
      break;
    case "usage":
      content = <Usage />;
      break;
    case "cost":
      content = <Cost />;
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
