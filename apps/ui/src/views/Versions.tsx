import { C } from "../tokens";
import { Card, SectionTitle, Chip, Dot, EmptyState } from "../primitives";
import { versionColor } from "../primitives";
import { useStore } from "../state/store";
import { VERSION_ROWS } from "../fixtures";

export function Versions() {
  const { state, dispatch } = useStore();
  if (state.level < 4) {
    return (
      <div>
        <SectionTitle title="Versions" />
        <EmptyState
          title="No environments yet"
          sub="Connect GitHub and AgentOS maps your main branch to @agentos and dev to @agentos-dev."
          ctaLabel="Connect GitHub"
          onCta={() => dispatch({ type: "connectGitHub" })}
        />
      </div>
    );
  }
  const grid = ".8fr 1fr 1fr .7fr 1fr 1.2fr";
  return (
    <div>
      <SectionTitle title="Versions" sub="main → @agentos · dev → @agentos-dev" />
      <Card>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: grid,
            gap: 12,
            padding: "0 0 12px",
            fontSize: 12,
            color: C.muted,
            borderBottom: "1px solid " + C.border,
          }}
        >
          {["Branch", "Version", "Deployed", "Eval", "Created by", "Status"].map((c) => (
            <div key={c}>{c}</div>
          ))}
        </div>
        {VERSION_ROWS.map((r, i) => (
          <div
            key={i}
            style={{
              display: "grid",
              gridTemplateColumns: grid,
              gap: 12,
              padding: "13px 0",
              alignItems: "center",
              borderBottom: "1px solid " + C.border,
              fontSize: 13.5,
            }}
          >
            <div>
              <Chip
                color={r.branch === "main" ? C.brand : C.warn}
                border={r.branch === "main" ? "rgba(62,207,142,.4)" : "rgba(191,135,0,.4)"}
              >
                {r.branch}
              </Chip>
            </div>
            <span style={{ fontFamily: C.mono, fontSize: 12.5 }}>{r.ver}</span>
            <span style={{ color: C.muted }}>{r.dep}</span>
            <span style={{ fontFamily: C.mono, color: C.text2 }}>{r.score}</span>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              {r.human ? (
                <div
                  style={{
                    width: 20,
                    height: 20,
                    borderRadius: "50%",
                    background: C.sel,
                    fontSize: 10,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    color: C.text2,
                  }}
                >
                  {r.by[0].toUpperCase()}
                </div>
              ) : (
                <div
                  style={{
                    width: 20,
                    height: 20,
                    borderRadius: 5,
                    background: "rgba(62,207,142,.15)",
                    fontSize: 11,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    color: C.brand,
                  }}
                >
                  ⚙
                </div>
              )}
              <span style={{ fontFamily: r.human ? "inherit" : C.mono, fontSize: r.human ? 13.5 : 12, color: C.text2 }}>{r.by}</span>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
              <Dot color={versionColor(r.state)} size={7} />
              <span style={{ fontSize: 12.5, color: C.text2 }}>{r.status}</span>
            </div>
          </div>
        ))}
      </Card>
    </div>
  );
}
