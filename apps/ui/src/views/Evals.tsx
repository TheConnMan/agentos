import { SectionTitle, EmptyState, Tabs } from "../primitives";
import { useStore } from "../state/store";
import type { EvalTab } from "../state/types";
import { Suite } from "./evals/Suite";
import { Matrix } from "./evals/Matrix";

const TABS: [EvalTab, string][] = [
  ["suite", "Suite"],
  ["matrix", "Matrix"],
];

export function Evals() {
  const { state, dispatch } = useStore();
  if (state.level < 4) {
    return (
      <div>
        <SectionTitle title="Evals" />
        <EmptyState
          title="No eval suite yet"
          sub="Connect GitHub to run your agent’s eval suite as a check on every pull request."
          ctaLabel="Connect GitHub"
          onCta={() => dispatch({ type: "connectGitHub" })}
        />
      </div>
    );
  }
  return (
    <div>
      <SectionTitle
        title="Evals"
        sub="Run your suite of fixed test cases against a version + model — on demand, on every PR, pinned per release. This is not live traffic (see Observability)."
      />
      <Tabs tabs={TABS} active={state.evalTab} onSelect={(id) => dispatch({ type: "setEvalTab", tab: id })} />
      {state.evalTab === "suite" ? <Suite /> : <Matrix />}
    </div>
  );
}
