// Fixture level 1-6 mirrors the design's six demo states:
// 1 fresh, 2 slack-connected, 3 agent-live, 4 agent-ci, 5 plugin, 6 fleet.
export type FixtureLevel = 1 | 2 | 3 | 4 | 5 | 6;

export type Nav =
  | "overview"
  | "agents"
  | "evals"
  | "observability"
  | "versions"
  | "connections"
  | "settings";

export type Env = "prod" | "dev";

export type ObsTab = "traces" | "metrics" | "logs" | "memory" | "usage" | "cost";
export type EvalTab = "suite" | "matrix";
export type MetricRange = "1h" | "6h" | "24h" | "7d";
export type ModalKind = "new-agent" | "plugin" | "slack-oauth";

// A plugin-format validator issue, surfaced inline when a wired deploy is
// rejected (mirrors api/client BundleIssue without importing across layers).
export interface DeployIssue {
  code: string;
  message: string;
  location: string;
}

export interface AppState {
  level: FixtureLevel;
  nav: Nav;
  env: Env;
  obsTab: ObsTab;
  evalTab: EvalTab;
  metricRange: MetricRange;
  modal: ModalKind | null;
  toast: string | null;
  tokenRevealed: boolean;
  provReveal: string | null;
  confetti: boolean;
  confettiDone: boolean;
  deploying: boolean;
  agentDeployed: boolean;
  pluginInstalled: boolean;
  pluginUploaded: boolean;
  matrixRun: boolean;
  extraEval: boolean;
  agentDetail: string | null;
  traceOpen: string | null;
  // When set (wired mode), the Traces list opens pre-filtered to this agent id;
  // null means all agents. Set by an agent card's "View traces" action.
  tracesAgentId: string | null;
  // When set, the Logs tab opens preselected to this runner pod / sandbox id.
  // Set by the trace detail's "View sandbox logs" action; null otherwise.
  logsPod: string | null;
  promoteForm: boolean;
  defaultModel: string;
  driftHover: string | null;
  slackTyping: boolean;
  showSuccess: boolean;
  // Wired-deploy feedback surfaced in the create-agent modal.
  deployIssues: DeployIssue[] | null;
  deployError: string | null;
  // Eval cases promoted from real traces (#259), newest first. Anonymized by the
  // API before they land here.
  promotedEvalCases: PromotedEvalCase[];
}

// An anonymized eval case promoted from a trace (mirrors the API EvalCaseOut).
export interface PromotedEvalCase {
  id: string;
  input: string;
  grader: { kind: "exact" | "contains" | "regex"; expected: string; case_sensitive: boolean };
}

export type Action =
  | { type: "setLevel"; level: FixtureLevel }
  | { type: "go"; nav: Nav }
  | { type: "openModal"; modal: ModalKind }
  | { type: "closeModal" }
  | { type: "toast"; message: string | null }
  | { type: "setEnv"; env: Env }
  | { type: "setObsTab"; tab: ObsTab }
  | { type: "viewTraces"; agentId: string | null }
  | { type: "setEvalTab"; tab: EvalTab }
  | { type: "setMetricRange"; range: MetricRange }
  | { type: "openTrace"; id: string }
  | { type: "closeTrace" }
  | { type: "openLogs"; sandboxId: string }
  | { type: "openAgentDetail"; id: string }
  | { type: "closeAgentDetail" }
  | { type: "deployStart" }
  | { type: "deployDone" }
  | { type: "confettiFire" }
  | { type: "deployFailedValidation"; issues: DeployIssue[] }
  | { type: "deployFailed"; message: string }
  | { type: "clearDeployErrors" }
  | { type: "allowSlack" }
  | { type: "pluginUpload" }
  | { type: "installPlugin" }
  | { type: "promoteFormOpen" }
  | { type: "promoteEval" }
  | { type: "addPromotedEvalCase"; evalCase: PromotedEvalCase }
  | { type: "runMatrix" }
  | { type: "reconfigureMatrix" }
  | { type: "revealToken"; value: boolean }
  | { type: "revealProvider"; id: string | null }
  | { type: "setDefaultModel"; model: string }
  | { type: "setDriftHover"; label: string | null }
  | { type: "slackTyping"; on: boolean }
  | { type: "confettiDone" }
  | { type: "enterAgentOS" }
  | { type: "connectGitHub" };
