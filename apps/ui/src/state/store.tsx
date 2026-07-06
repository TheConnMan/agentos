import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  type Dispatch,
  type ReactNode,
} from "react";
import type { Action, AppState, FixtureLevel } from "./types";

const max = (a: FixtureLevel, b: FixtureLevel): FixtureLevel =>
  (a > b ? a : b) as FixtureLevel;

export function initialState(level: FixtureLevel): AppState {
  return {
    level,
    nav: "overview",
    env: "prod",
    terminal: false,
    obsTab: "traces",
    evalTab: "suite",
    metricRange: "6h",
    modal: null,
    toast: null,
    tokenRevealed: false,
    provReveal: null,
    confetti: false,
    confettiDone: level >= 3,
    deploying: false,
    agentDeployed: level >= 3,
    pluginInstalled: level >= 5,
    pluginUploaded: false,
    matrixRun: false,
    extraEval: false,
    agentDetail: null,
    traceOpen: null,
    promoteForm: false,
    defaultModel: "claude-sonnet-5",
    driftHover: null,
    slackTyping: false,
    showSuccess: false,
    deployIssues: null,
    deployError: null,
  };
}

// Reset shared to setLevel: mirrors the canon's setLevel() which snaps every
// transient sub-view back to its default when the fixture level changes.
function levelReset(s: AppState, level: FixtureLevel): AppState {
  return {
    ...s,
    level,
    nav: "overview",
    modal: null,
    agentDetail: null,
    terminal: false,
    matrixRun: false,
    traceOpen: null,
    obsTab: "traces",
    promoteForm: false,
    agentDeployed: level >= 3,
    pluginInstalled: level >= 5,
    extraEval: false,
    showSuccess: false,
    env: level < 4 ? "prod" : s.env,
  };
}

export function reducer(s: AppState, a: Action): AppState {
  switch (a.type) {
    case "setLevel":
      return levelReset(s, a.level);
    case "go":
      return { ...s, nav: a.nav, terminal: false, agentDetail: null, traceOpen: null };
    case "openModal":
      return { ...s, modal: a.modal };
    case "closeModal":
      return { ...s, modal: null, pluginUploaded: false, deployIssues: null, deployError: null };
    case "toast":
      return { ...s, toast: a.message };
    case "setEnv":
      return { ...s, env: a.env };
    case "toggleTerminal":
      return { ...s, terminal: !s.terminal };
    case "setObsTab":
      return { ...s, obsTab: a.tab, traceOpen: null };
    case "setEvalTab":
      return { ...s, evalTab: a.tab };
    case "setMetricRange":
      return { ...s, metricRange: a.range };
    case "openTrace":
      return { ...s, traceOpen: a.id };
    case "closeTrace":
      return { ...s, traceOpen: null, promoteForm: false };
    case "openAgentDetail":
      return { ...s, agentDetail: a.id };
    case "closeAgentDetail":
      return { ...s, agentDetail: null, nav: "overview" };
    case "deployStart":
      return { ...s, deploying: true, deployIssues: null, deployError: null };
    case "deployDone": {
      const fire = !s.confettiDone;
      return {
        ...s,
        deploying: false,
        modal: null,
        pluginUploaded: false,
        nav: "overview",
        level: max(s.level, 3),
        agentDeployed: true,
        showSuccess: true,
        confetti: fire,
        confettiDone: true,
        deployIssues: null,
        deployError: null,
      };
    }
    case "confettiFire":
      // Backend-driven deploy success: close the modal, land on Overview, and
      // fire confetti once, WITHOUT the fixture level/showSuccess machinery (the
      // wired shell renders its own honest post-deploy panel from real state).
      return {
        ...s,
        modal: null,
        deploying: false,
        deployIssues: null,
        deployError: null,
        nav: "overview",
        terminal: false,
        agentDetail: null,
        confetti: !s.confettiDone,
        confettiDone: true,
      };
    case "deployFailedValidation":
      return { ...s, deploying: false, deployIssues: a.issues, deployError: null };
    case "deployFailed":
      return { ...s, deploying: false, deployError: a.message, deployIssues: null };
    case "clearDeployErrors":
      return s.deployIssues || s.deployError ? { ...s, deployIssues: null, deployError: null } : s;
    case "allowSlack":
      return {
        ...s,
        modal: null,
        level: max(s.level, 2),
        toast: "Slack connected",
      };
    case "pluginUpload":
      return { ...s, pluginUploaded: true };
    case "installPlugin":
      return {
        ...s,
        modal: null,
        level: max(s.level, 5),
        pluginInstalled: true,
        pluginUploaded: false,
        nav: "agents",
        toast: "Plugin installed · sre-triage",
      };
    case "promoteFormOpen":
      return { ...s, promoteForm: true };
    case "promoteEval":
      return { ...s, extraEval: true, promoteForm: false, toast: "Eval suite: 36 → 37 cases" };
    case "runMatrix":
      return { ...s, matrixRun: true };
    case "reconfigureMatrix":
      return { ...s, matrixRun: false };
    case "revealToken":
      return { ...s, tokenRevealed: a.value };
    case "revealProvider":
      return { ...s, provReveal: a.id };
    case "setDefaultModel":
      return { ...s, defaultModel: a.model };
    case "setDriftHover":
      return { ...s, driftHover: a.label };
    case "slackTyping":
      return { ...s, slackTyping: a.on };
    case "confettiDone":
      return { ...s, confetti: false };
    case "connectGitHub":
      return {
        ...s,
        level: max(s.level, 4),
        agentDeployed: true,
        toast: "GitHub connected · CI evals enabled",
      };
    case "enterAgentOS":
      return { ...s, level: max(s.level, 3), agentDeployed: true, nav: "overview", showSuccess: true };
    default:
      return s;
  }
}

interface Store {
  state: AppState;
  dispatch: Dispatch<Action>;
  slackOn: boolean;
  ghOn: boolean;
  envDev: boolean;
}

const StoreContext = createContext<Store | null>(null);

export function readLevelFromUrl(search: string): FixtureLevel {
  const p = new URLSearchParams(search);
  const raw = Number(p.get("state"));
  if (raw >= 1 && raw <= 6) return raw as FixtureLevel;
  return 1;
}

export function StoreProvider({
  children,
  level,
}: {
  children: ReactNode;
  level?: FixtureLevel;
}) {
  const start =
    level ??
    (typeof window !== "undefined" ? readLevelFromUrl(window.location.search) : 1);
  const [state, dispatch] = useReducer(reducer, start, initialState);

  // Auto-clear toasts after 2.5s, matching the canon's toast lifetime.
  useEffect(() => {
    if (!state.toast) return;
    const id = setTimeout(() => dispatch({ type: "toast", message: null }), 2500);
    return () => clearTimeout(id);
  }, [state.toast]);

  const value = useMemo<Store>(
    () => ({
      state,
      dispatch,
      slackOn: state.level >= 2,
      ghOn: state.level >= 4,
      envDev: state.level >= 4 && state.env === "dev",
    }),
    [state],
  );

  return <StoreContext.Provider value={value}>{children}</StoreContext.Provider>;
}

export function useStore(): Store {
  const s = useContext(StoreContext);
  if (!s) throw new Error("useStore must be used within StoreProvider");
  return s;
}
