import { C } from "../tokens";
import { useStore } from "../state/store";
import type { FixtureLevel } from "../state/types";

const STATES: [FixtureLevel, string][] = [
  [1, "1 Fresh"],
  [2, "2 Slack"],
  [3, "3 Live"],
  [4, "4 CI"],
  [5, "5 Plugin"],
  [6, "6 Fleet"],
];

// Internal fixture-state switcher for development and E2E. It is NOT the design's
// demo-controls bar (that was prototype scaffolding, deliberately not shipped).
// It only renders when the URL carries ?dev=1, so the product view stays clean;
// Playwright otherwise drives states directly via ?state=N on load.
export function StateSwitcher() {
  const { state, dispatch } = useStore();
  const enabled = typeof window !== "undefined" && new URLSearchParams(window.location.search).get("dev") === "1";
  if (!enabled) return null;
  return (
    <div
      data-testid="state-switcher"
      style={{
        position: "fixed",
        bottom: 16,
        left: 16,
        zIndex: 11000,
        display: "flex",
        gap: 6,
        background: C.card,
        border: "1px solid " + C.borderStrong,
        borderRadius: 10,
        padding: 6,
        boxShadow: "0 10px 24px rgba(0,0,0,.4)",
      }}
    >
      {STATES.map(([lvl, label]) => {
        const active = state.level === lvl;
        return (
          <button
            key={lvl}
            type="button"
            data-testid={`switch-state-${lvl}`}
            onClick={() => dispatch({ type: "setLevel", level: lvl })}
            style={{
              fontSize: 11,
              fontFamily: C.mono,
              padding: "4px 8px",
              borderRadius: 6,
              cursor: "pointer",
              background: active ? C.brand : "transparent",
              color: active ? "#08120c" : C.text2,
              border: "1px solid " + (active ? C.brand : C.border),
              fontWeight: active ? 600 : 400,
            }}
          >
            {label}
          </button>
        );
      })}
    </div>
  );
}
