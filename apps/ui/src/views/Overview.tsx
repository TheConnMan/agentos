import { C } from "../tokens";
import { Card, SectionTitle, Button, Dot, Chip, Sparkline } from "../primitives";
import { healthColor } from "../primitives";
import { hoverBg } from "../lib/style";
import { useStore } from "../state/store";
import { agentsForLevel, ALL_AGENTS } from "../fixtures";
import { SlackCard } from "../components/SlackCard";

// The success banner + Slack evidence card shown once, right after the first
// deploy (the magic moment). Ported from successPanel().
function SuccessPanel() {
  const { envDev } = useStore();
  return (
    <div style={{ marginBottom: 24 }}>
      <div
        style={{
          background: "rgba(62,207,142,.07)",
          border: "1px solid rgba(62,207,142,.3)",
          borderRadius: 14,
          padding: "20px 22px",
          marginBottom: 16,
          display: "flex",
          alignItems: "flex-start",
          gap: 14,
        }}
      >
        <div
          style={{
            width: 34,
            height: 34,
            borderRadius: 9,
            background: "rgba(62,207,142,.15)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: C.brand,
            fontSize: 16,
            flexShrink: 0,
          }}
        >
          ✓
        </div>
        <div>
          <div style={{ fontSize: 16, fontWeight: 500, marginBottom: 3 }}>
            <span style={{ fontFamily: C.mono, color: C.brand }}>{envDev ? "@agentos-dev" : "@agentos"}</span> is
            live in #revenue-ops
          </div>
          <div style={{ fontSize: 13.5, color: C.text2 }}>
            replied to its first ping in <strong style={{ color: C.text }}>42ms</strong> · observability is already on
          </div>
        </div>
      </div>
      <SlackCard variant={envDev ? "dev" : "prod"} />
    </div>
  );
}

// Fresh-account setup checklist (Stripe go-live pattern). Replaces the deferred
// full-screen onboarding wizard: the create-agent modal is the deploy path.
function SetupChecklist() {
  const { state, dispatch } = useStore();
  const items = [
    { label: "Connect Slack", done: state.level >= 2, cta: () => dispatch({ type: "openModal", modal: "slack-oauth" }) },
    { label: "Create your first agent", done: state.level >= 3, cta: () => dispatch({ type: "openModal", modal: "new-agent" }) },
    { label: "Send it a message", done: state.level >= 3, cta: () => dispatch({ type: "openModal", modal: "new-agent" }) },
    { label: "Connect GitHub for CI evals", done: state.level >= 4, cta: () => dispatch({ type: "connectGitHub" }) },
  ];
  const firstOpen = items.findIndex((it) => !it.done);
  return (
    <div>
      <SectionTitle title="Welcome to AgentOS" sub="Four steps to your first agent live in Slack." />
      <Card style={{ maxWidth: 560 }}>
        {items.map((it, i) => {
          const isNext = i === firstOpen;
          return (
            <div
              key={it.label}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 14,
                padding: "14px 0",
                borderTop: i ? "1px solid " + C.border : "none",
              }}
            >
              <span
                style={{
                  width: 22,
                  height: 22,
                  borderRadius: "50%",
                  border: "1.5px solid " + (it.done ? C.brand : C.borderStrong),
                  background: it.done ? C.brand : "transparent",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  color: "#08120c",
                  fontSize: 12,
                  flexShrink: 0,
                }}
              >
                {it.done ? "✓" : ""}
              </span>
              <span style={{ flex: 1, fontSize: 14, color: it.done ? C.muted : C.text, textDecoration: it.done ? "line-through" : "none" }}>
                {it.label}
              </span>
              {!it.done && isNext ? <Button label="Start" variant="primary" size="sm" onClick={it.cta} /> : null}
              {!it.done && !isNext ? <span style={{ fontSize: 12, color: C.disabled, fontFamily: C.mono }}>up next</span> : null}
            </div>
          );
        })}
      </Card>
    </div>
  );
}

function Fleet() {
  const { dispatch } = useStore();
  const cols = ["Agent", "Channel(s)", "Version", "Eval trend", "Runs today", "Cost today", "Health"];
  const grid = "1.3fr 1fr 1.1fr .9fr .8fr .8fr .7fr";
  const stats: [string, string][] = [
    ["Agents", "5"],
    ["Runs today", "785"],
    ["Avg eval score", "92%"],
    ["Cost today", "$21.40"],
  ];
  return (
    <div>
      <SectionTitle title="Overview" sub="acme-corp fleet · 5 agents" />
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 14, marginBottom: 22 }}>
        {stats.map((s) => (
          <Card key={s[0]} style={{ padding: "16px 18px" }}>
            <div style={{ fontSize: 12, color: C.muted, marginBottom: 6 }}>{s[0]}</div>
            <div style={{ fontSize: 24, fontWeight: 400, fontFamily: C.mono }}>{s[1]}</div>
          </Card>
        ))}
      </div>
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
          {cols.map((c) => (
            <div key={c}>{c}</div>
          ))}
        </div>
        {ALL_AGENTS.map((a) => (
          <button
            key={a.id}
            type="button"
            onClick={() => dispatch({ type: "openAgentDetail", id: a.id })}
            style={{
              display: "grid",
              gridTemplateColumns: grid,
              gap: 12,
              padding: "13px 0",
              alignItems: "center",
              borderBottom: "1px solid " + C.border,
              background: "transparent",
              border: "none",
              width: "100%",
              textAlign: "left",
              cursor: "pointer",
              color: C.text,
              fontSize: 13.5,
            }}
            {...hoverBg("transparent", C.hover)}
          >
            <div style={{ fontWeight: 500, fontFamily: C.mono, fontSize: 13, display: "flex", alignItems: "center", gap: 8 }}>
              {a.id}
              {a.plugin ? <Chip color={C.mutedStatus}>plugin</Chip> : null}
            </div>
            <div style={{ color: C.text2, fontFamily: C.mono, fontSize: 12.5 }}>{a.ch}</div>
            <div style={{ display: "flex", gap: 5 }}>
              <Chip color={C.brand} border="rgba(62,207,142,.4)">
                {a.prodV}
              </Chip>
              <Chip color={C.warn} border="rgba(191,135,0,.4)">
                {a.devV}
              </Chip>
            </div>
            <Sparkline data={a.trend} color={a.health === "amber" ? C.warn : C.brand} />
            <div style={{ fontFamily: C.mono, color: C.text2 }}>{a.runs}</div>
            <div style={{ fontFamily: C.mono, color: C.text2 }}>{a.cost}</div>
            <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
              <Dot color={healthColor(a.health)} size={8} />
              <span style={{ fontSize: 12, color: C.muted, textTransform: "capitalize" }}>{a.health}</span>
            </div>
          </button>
        ))}
      </Card>
    </div>
  );
}

export function Overview() {
  const { state, dispatch, ghOn } = useStore();

  if (state.level === 6) return <Fleet />;
  if (state.level < 3) return <SetupChecklist />;

  const agents = agentsForLevel(state.level);
  const n = agents.length;
  const stats: [string, string][] = ghOn
    ? [["Runs today", "128"], ["Median latency", "420ms"], ["Eval score", "94%"], ["Cost today", "$4.20"]]
    : [["Runs today", "1"], ["Median latency", "420ms"], ["Eval score", "—"], ["Cost today", "$0.04"]];
  const recentAll = [
    { msg: "@agentos approve Meridian at 18%?", by: "mara", st: "ok" as const, when: "2m ago", dur: "2.1s" },
    { msg: "@agentos Northwind renewal terms?", by: "jt", st: "ok" as const, when: "14m ago", dur: "1.4s" },
    { msg: "@agentos approve Northwind at 22%?", by: "priya", st: ghOn ? ("fail" as const) : ("ok" as const), when: "1h ago", dur: "1.8s" },
    { msg: "@agentos who signs off above 20%?", by: "sam", st: "ok" as const, when: "2h ago", dur: "0.9s" },
  ];
  const recent = recentAll.slice(0, ghOn ? 4 : 1);
  const goTraces = () => dispatch({ type: "setObsTab", tab: "traces" });
  const goObs = () => dispatch({ type: "go", nav: "observability" });

  return (
    <div>
      {state.showSuccess ? <SuccessPanel /> : null}
      <SectionTitle title="Overview" sub={"acme-corp · " + n + (n === 1 ? " agent live" : " agents live")} />
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 14, marginBottom: 22 }}>
        {stats.map((s) => (
          <Card key={s[0]} style={{ padding: "16px 18px" }}>
            <div style={{ fontSize: 12, color: C.muted, marginBottom: 6 }}>{s[0]}</div>
            <div style={{ fontSize: 24, fontWeight: 400, fontFamily: C.mono }}>{s[1]}</div>
          </Card>
        ))}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1.5fr 1fr", gap: 16 }}>
        <Card>
          <div style={{ display: "flex", alignItems: "center", marginBottom: 8 }}>
            <div style={{ fontSize: 14, fontWeight: 500 }}>Recent activity</div>
            <button
              type="button"
              onClick={() => {
                goObs();
                goTraces();
              }}
              style={{ marginLeft: "auto", background: "none", border: "none", color: C.link, fontSize: 12.5, cursor: "pointer" }}
            >
              All traces →
            </button>
          </div>
          {recent.map((r, i) => {
            const bad = r.st === "fail";
            return (
              <button
                key={i}
                type="button"
                onClick={() => {
                  goObs();
                  goTraces();
                }}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                  padding: "11px 0",
                  width: "100%",
                  textAlign: "left",
                  background: "transparent",
                  border: "none",
                  borderTop: i ? "1px solid " + C.border : "none",
                  cursor: "pointer",
                  color: C.text,
                }}
                {...hoverBg("transparent", C.hover)}
              >
                <Dot color={bad ? C.destructive : C.success} size={7} />
                <span style={{ flex: 1, fontSize: 13, color: C.text2, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                  {r.msg}
                </span>
                <span style={{ fontSize: 12, color: C.muted, fontFamily: C.mono }}>{r.dur}</span>
                <span style={{ fontSize: 12, color: C.muted, width: 64, textAlign: "right" }}>{r.when}</span>
              </button>
            );
          })}
        </Card>
        <Card>
          <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 14 }}>Agents</div>
          {agents.map((a, i) => (
            <button
              key={a.id}
              type="button"
              onClick={() => dispatch({ type: "go", nav: "agents" })}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
                padding: "10px 0",
                width: "100%",
                textAlign: "left",
                background: "transparent",
                border: "none",
                borderTop: i ? "1px solid " + C.border : "none",
                cursor: "pointer",
                color: C.text,
              }}
              {...hoverBg("transparent", C.hover)}
            >
              <Dot color={C.success} size={8} />
              <span style={{ flex: 1, fontFamily: C.mono, fontSize: 13 }}>{a.id}</span>
              <span style={{ fontSize: 12, color: C.muted, fontFamily: C.mono }}>{ghOn ? a.score + "%" : "live"}</span>
            </button>
          ))}
          <div style={{ marginTop: 14, paddingTop: 14, borderTop: "1px solid " + C.border }}>
            <Button
              label={ghOn ? "Open Observability" : "Connect GitHub for CI evals"}
              full
              onClick={ghOn ? () => dispatch({ type: "go", nav: "observability" }) : () => dispatch({ type: "connectGitHub" })}
            />
          </div>
        </Card>
      </div>
    </div>
  );
}
