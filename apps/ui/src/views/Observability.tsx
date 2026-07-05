import { SectionTitle, EmptyState, Tabs } from "../primitives";
import { useStore } from "../state/store";
import type { ObsTab } from "../state/types";
import { TracesList, TraceDetail } from "./obs/Traces";
import { Metrics } from "./obs/Metrics";
import { Logs } from "./obs/Logs";
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
      content = state.traceOpen ? <TraceDetail /> : <TracesList />;
      break;
    case "metrics":
      content = <Metrics />;
      break;
    case "logs":
      content = <Logs />;
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
