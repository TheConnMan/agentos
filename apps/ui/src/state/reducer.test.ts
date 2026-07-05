import { describe, expect, it } from "vitest";
import { initialState, reducer } from "./store";
import type { AppState } from "./types";

const at = (level: 1 | 2 | 3 | 4 | 5 | 6): AppState => initialState(level);

describe("reducer state machine", () => {
  it("setLevel snaps derived flags and resets transient views", () => {
    let s = at(3);
    s = reducer(s, { type: "openTrace", id: "tr_8f3k21" });
    s = reducer(s, { type: "setObsTab", tab: "metrics" });
    s = reducer(s, { type: "setLevel", level: 6 });
    expect(s.level).toBe(6);
    expect(s.agentDeployed).toBe(true);
    expect(s.pluginInstalled).toBe(true);
    expect(s.traceOpen).toBeNull();
    expect(s.obsTab).toBe("traces");
    expect(s.nav).toBe("overview");
  });

  it("deployDone advances to at least level 3, shows success, fires confetti once", () => {
    let s = at(2);
    s = reducer(s, { type: "deployStart" });
    expect(s.deploying).toBe(true);
    s = reducer(s, { type: "deployDone" });
    expect(s.deploying).toBe(false);
    expect(s.level).toBe(3);
    expect(s.agentDeployed).toBe(true);
    expect(s.showSuccess).toBe(true);
    expect(s.confetti).toBe(true);
    expect(s.confettiDone).toBe(true);

    // A second deploy does not re-fire confetti.
    s = reducer(s, { type: "confettiDone" });
    s = reducer(s, { type: "deployDone" });
    expect(s.confetti).toBe(false);
  });

  it("deployDone never lowers the level", () => {
    const s = reducer(at(5), { type: "deployDone" });
    expect(s.level).toBe(5);
  });

  it("allowSlack connects Slack and advances to at least level 2", () => {
    const s = reducer(at(1), { type: "allowSlack" });
    expect(s.level).toBe(2);
    expect(s.modal).toBeNull();
    expect(s.toast).toBe("Slack connected");
  });

  it("installPlugin advances to level 5 and lands on agents", () => {
    const s = reducer(at(4), { type: "installPlugin" });
    expect(s.level).toBe(5);
    expect(s.pluginInstalled).toBe(true);
    expect(s.nav).toBe("agents");
  });

  it("promote flow opens the form then adds the eval case", () => {
    let s = reducer(at(4), { type: "promoteFormOpen" });
    expect(s.promoteForm).toBe(true);
    s = reducer(s, { type: "promoteEval" });
    expect(s.extraEval).toBe(true);
    expect(s.promoteForm).toBe(false);
    expect(s.toast).toContain("37");
  });

  it("matrix run and reconfigure toggle matrixRun", () => {
    let s = reducer(at(4), { type: "runMatrix" });
    expect(s.matrixRun).toBe(true);
    s = reducer(s, { type: "reconfigureMatrix" });
    expect(s.matrixRun).toBe(false);
  });

  it("connectGitHub reaches level 4 for CI", () => {
    const s = reducer(at(3), { type: "connectGitHub" });
    expect(s.level).toBe(4);
    expect(s.toast).toContain("GitHub connected");
  });

  it("env stays prod below level 4 even if set to dev", () => {
    const s = reducer(at(3), { type: "setLevel", level: 3 });
    expect(s.env).toBe("prod");
  });
});
