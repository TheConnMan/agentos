import type { ReactNode } from "react";
import { C } from "../tokens";
import { Dot } from "../primitives";
import { hoverBg } from "../lib/style";
import { useStore } from "../state/store";

// The single Slack evidence card: window-chrome card with the human ping and the
// guardrailed bot reply. `variant` re-tints the APP badge for the dev-bot story.
// This is the ONLY Slack chrome in the product (no workspace sidebar, no composer).
export function SlackCard({ variant }: { variant: "prod" | "dev" }) {
  const { state, dispatch } = useStore();
  const dev = variant === "dev";
  const handle = dev ? "@agentos-dev" : "@agentos";
  const badgeBg = dev ? "rgba(191,135,0,.18)" : "rgba(62,207,142,.18)";
  const badgeColor = dev ? C.warn : C.brand;

  const msg = (avatar: string, avBg: string, name: string, appBadge: boolean, body: ReactNode, self: boolean) => (
    <div style={{ display: "flex", gap: 10, padding: "8px 0" }}>
      <div
        style={{
          width: 32,
          height: 32,
          borderRadius: 7,
          background: avBg,
          flexShrink: 0,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: 13,
          fontWeight: 600,
          color: "#0d0d0d",
        }}
      >
        {avatar}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 7, marginBottom: 2 }}>
          <span style={{ fontWeight: 700, fontSize: 13.5, color: "#e8e8e8" }}>{name}</span>
          {appBadge ? (
            <span
              style={{
                fontSize: 9.5,
                fontWeight: 700,
                letterSpacing: 0.5,
                padding: "1px 5px",
                borderRadius: 3,
                background: badgeBg,
                color: badgeColor,
              }}
            >
              APP
            </span>
          ) : null}
          <span style={{ fontSize: 11, color: "#8a8d91" }}>{self ? "14:02" : "14:01"}</span>
        </div>
        <div style={{ fontSize: 13.5, color: "#d1d2d3", lineHeight: 1.5 }}>{body}</div>
      </div>
    </div>
  );

  return (
    <div
      style={{
        width: 392,
        background: "#1a1d21",
        borderRadius: 12,
        overflow: "hidden",
        border: "1px solid #26292e",
        boxShadow: "0 12px 28px rgba(0,0,0,.45)",
      }}
    >
      <div
        style={{
          background: "#161B22",
          padding: "9px 14px",
          display: "flex",
          alignItems: "center",
          gap: 8,
          borderBottom: "1px solid #26292e",
        }}
      >
        <div style={{ display: "flex", gap: 7 }}>
          <Dot color="#ED6A5F" size={12} />
          <Dot color="#F6BE50" size={12} />
          <Dot color="#61C554" size={12} />
        </div>
        <span style={{ marginLeft: 6, fontSize: 12, color: "#8a8d91" }}>Slack — #revenue-ops</span>
      </div>
      <div style={{ padding: "8px 16px 14px" }}>
        {msg("M", "#c8a2f0", "mara", false, <span>{handle} can we approve the Meridian deal at 18% discount?</span>, false)}
        {msg(
          "R",
          dev ? "#e8c76b" : C.brand,
          "agentos",
          true,
          <div>
            <div>
              Meridian Corp · $84,000 · 18% requested ·{" "}
              <strong style={{ color: dev ? C.warn : "#e8e8e8" }}>Needs approval</strong>
            </div>
            <div style={{ marginTop: 3, color: "#a9abae" }}>
              Policy caps auto-approve at 15%. Routed to approver from policy: J. Whitfield.
            </div>
          </div>,
          true,
        )}
        <div
          style={{
            fontSize: 11.5,
            color: "#6b6e73",
            fontStyle: "italic",
            padding: "6px 0 2px",
            borderTop: "1px solid #23262b",
            marginTop: 6,
          }}
        >
          only visible to you: trace <span style={{ fontFamily: C.mono }}>tr_8f3k21</span>
        </div>
      </div>
      <div style={{ padding: "0 16px 14px" }}>
        <button
          type="button"
          onClick={() => {
            dispatch({ type: "slackTyping", on: true });
            setTimeout(() => dispatch({ type: "slackTyping", on: false }), 1600);
          }}
          style={{
            fontSize: 12.5,
            padding: "6px 12px",
            borderRadius: 7,
            cursor: "pointer",
            background: "transparent",
            border: "1px solid #33363b",
            color: "#b7b9bc",
          }}
          {...hoverBg("transparent", "#22252a")}
        >
          {state.slackTyping ? "● ● ●  typing…" : "Send test message"}
        </button>
      </div>
    </div>
  );
}
