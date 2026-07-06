import { useEffect, useMemo, useRef, useState } from "react";
import { C } from "../tokens";
import { SectionTitle, Dot, Chip } from "../primitives";
import { hoverBg } from "../lib/style";

// Terminal entry model. The CLI view is a self-contained REPL (local state, not
// the app store) that mirrors the product workflow. Ported from the canon's
// terminalView() + resolveCmd()/botReply()/runCmd().
type LineTone = "out" | "dim" | "success" | "error";
type Entry =
  | { type: "cmd"; text: string }
  | { type: "line"; t: LineTone; text: string }
  | { type: "trace"; text: string }
  | { type: "msg"; who: "user" | "bot"; text?: string; head?: string; body?: string };

const L = (t: LineTone, text: string): Entry => ({ type: "line", t, text });

function startBox(): Entry[] {
  return [
    L("out", "╭─ agentos dev environment ──────────────────────────╮"),
    L("out", "│  Local bot        http://localhost:7245          │"),
    L("out", '│  Slack emulator   agentos send "<message>"         │'),
    L("out", "│  Eval runner      agentos eval                     │"),
    L("out", "│  Version          v1.4.2 · claude-sonnet-4.5     │"),
    L("out", "╰──────────────────────────────────────────────────╯"),
  ];
}

function seedTerm(): Entry[] {
  return [
    L("dim", "agentos cli v0 · acme-corp · env prod → @agentos"),
    ...startBox(),
    L("dim", 'Type a command, or tap one below. Try: agentos send "@agentos ..."'),
  ];
}

function botReply(msg: string): { head: string; body: string; tools: number } {
  const lc = msg.toLowerCase();
  const pm = msg.match(/(\d+)\s*%/);
  const pct = pm ? parseInt(pm[1], 10) : null;
  if (pct != null) {
    const d = /northwind/i.test(msg)
      ? { n: "Northwind", amt: "$23,500" }
      : { n: "Meridian Corp", amt: "$84,000" };
    if (pct > 15)
      return {
        head: d.n + " · " + d.amt + " · " + pct + "% requested · Needs approval",
        body: "Policy caps auto-approve at 15%. Routed to approver from policy.yaml: J. Whitfield.",
        tools: 3,
      };
    return {
      head: d.n + " · " + d.amt + " · " + pct + "% requested · Auto-approved",
      body: "Within the 15% policy cap. Logged to the CRM deal record.",
      tools: 2,
    };
  }
  if (lc.includes("who") && lc.includes("approv"))
    return { head: "Approvers come from policy.yaml", body: "Discounts above 15% route to J. Whitfield (VP Sales). I never invent an approver.", tools: 1 };
  if (lc.includes("what can you") || lc === "@agentos help" || lc.includes("help me"))
    return { head: "I handle deal-desk approvals in #revenue-ops", body: "Ask me to approve or route a discount — I check the CRM record and policy.yaml.", tools: 0 };
  return { head: "I can only act on deal-desk requests here", body: "Try: “@agentos can we approve the Northwind deal at 12%?”", tools: 0 };
}

function resolveCmd(cmd: string): Entry[] {
  const lc = cmd.toLowerCase().trim();
  if (lc === "help")
    return [
      L("dim", "commands"),
      L("out", "  agentos init            scaffold a project"),
      L("out", "  agentos start           run the local dev bot"),
      L("out", "  agentos dev             hot-reload skills"),
      L("out", '  agentos send "<msg>"    message the bot (threads)'),
      L("out", "  agentos eval            run the eval suite"),
      L("out", "  agentos deploy          deploy to Slack"),
      L("out", "  agentos status          agent, model & env"),
      L("out", "  agentos logs            tail recent logs"),
      L("out", "  git push origin dev   push & queue eval check"),
      L("out", "  clear                 clear the screen"),
    ];
  if (lc === "agentos init") return [L("dim", "Creating agentos.toml, skills/, evals/ …"), L("success", "✓ Project initialized · edit skills/deal-desk/skill.md")];
  if (lc === "agentos start") return startBox();
  if (lc === "agentos dev") return [L("success", "✓ hot reload · watching skills/"), L("dim", "bot listening on http://localhost:7245")];
  if (lc === "agentos eval")
    return [
      L("dim", "Running suite deal-desk core (36 cases) · model claude-sonnet-4.5 …"),
      L("success", "✓ approver-from-policy-source            1.2s"),
      L("error", "✗ deal-data-from-crm-not-slack           0.9s"),
      L("success", "✓ no-discount-above-policy-cap           1.1s"),
      L("dim", "  …31 more"),
      L("error", "✗ formats-verdict-structured             1.0s"),
      L("dim", "─────────────────────────────"),
      L("out", "34/36 passed · 2 failures · 12.4s"),
    ];
  if (lc === "agentos deploy") return [L("dim", "Bundling deal-desk · uploading …"), L("success", "✓ @agentos deployed to #revenue-ops · v1.4.2")];
  if (lc === "agentos status")
    return [
      L("out", "agent    deal-desk"),
      L("out", "env      prod  →  @agentos"),
      L("out", "model    claude-sonnet-4.5"),
      L("out", "version  v1.4.2  ·  eval 94%"),
      L("out", "channels #revenue-ops"),
    ];
  if (lc === "agentos logs")
    return [
      L("dim", "14:02:07 info  request received user=mara"),
      L("dim", "14:02:08 info  verdict routed to J. Whitfield"),
      L("dim", "14:03:41 warn  latency above p95 (5.2s)"),
    ];
  if (lc.startsWith("git push"))
    return [
      L("dim", "Enumerating objects: 12, done."),
      L("dim", "To github.com:acme/agentos-agents.git"),
      L("dim", "   4f2c91a..b7e02d1  dev -> dev"),
      L("success", "→ agentos: eval check queued on PR #42"),
    ];
  if (lc.startsWith("agentos send")) {
    const m = cmd.match(/"([^"]*)"|'([^']*)'/);
    const msg = (m && (m[1] || m[2])) || cmd.replace(/^agentos send\s*/i, "").trim();
    if (!msg) return [L("error", 'usage: agentos send "<message>"')];
    const r = botReply(msg);
    const tid = "tr_" + Math.random().toString(16).slice(2, 8);
    const dur = (0.8 + Math.random() * 1.6).toFixed(1);
    const cost = (0.02 + Math.random() * 0.06).toFixed(2);
    return [
      { type: "msg", who: "user", text: msg },
      { type: "msg", who: "bot", head: r.head, body: r.body },
      { type: "trace", text: "→ trace " + tid + " · " + r.tools + " tool calls · " + dur + "s · $" + cost },
    ];
  }
  return [L("error", "agentos: command not found: " + cmd.split(" ")[0]), L("dim", "try 'help'")];
}

function EntryRow({ e }: { e: Entry }) {
  if (e.type === "cmd")
    return (
      <div style={{ display: "flex", whiteSpace: "pre-wrap", marginTop: 8 }}>
        <span style={{ color: C.brand, fontWeight: 700, flexShrink: 0 }}>❯ </span>
        <span style={{ color: "#e6edf3" }}>{e.text}</span>
      </div>
    );
  if (e.type === "line") {
    const color = e.t === "success" ? C.success : e.t === "error" ? C.failure : e.t === "dim" ? C.mutedStatus : "#c9d1d9";
    return <div style={{ color, whiteSpace: "pre-wrap" }}>{e.text}</div>;
  }
  if (e.type === "trace") return <div style={{ color: C.mutedStatus, fontStyle: "italic", paddingLeft: 14, marginBottom: 2 }}>{e.text}</div>;
  const bot = e.who === "bot";
  return (
    <div
      style={{
        display: "flex",
        gap: 8,
        padding: "3px 0 3px 12px",
        borderLeft: "2px solid " + (bot ? "rgba(62,207,142,.5)" : C.borderStrong),
        marginLeft: 2,
      }}
    >
      <span style={{ color: bot ? C.brand : C.mutedStatus, fontWeight: 700, flexShrink: 0 }}>{bot ? "agentos ›" : "you ›"}</span>
      {bot ? (
        <div>
          <div style={{ color: "#e6edf3" }}>{e.head}</div>
          {e.body ? <div style={{ color: "#a9abae" }}>{e.body}</div> : null}
        </div>
      ) : (
        <span style={{ color: "#d1d2d3" }}>{e.text}</span>
      )}
    </div>
  );
}

const CHIPS: [string, boolean][] = [
  ["agentos eval", true],
  ["agentos status", true],
  ['agentos send "@agentos approve Northwind at 12%?"', false],
  ["agentos logs", true],
  ["help", true],
  ["clear", true],
];

export function Terminal() {
  const [log, setLog] = useState<Entry[]>(() => seedTerm());
  const [input, setInput] = useState("");
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    inputRef.current?.focus();
  }, [log]);

  const run = (raw: string) => {
    const cmd = raw.trim();
    if (!cmd) return;
    if (cmd === "clear") {
      setLog([]);
      setInput("");
      return;
    }
    setLog((prev) => [...prev, { type: "cmd", text: cmd }, ...resolveCmd(cmd)]);
    setInput("");
  };

  const rows = useMemo(() => log.map((e, i) => <EntryRow key={i} e={e} />), [log]);

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16 }}>
        <SectionTitle title="CLI view" />
        <div style={{ marginTop: -14, display: "flex", alignItems: "center", gap: 10 }}>
          <Chip color={C.warn} border="rgba(191,135,0,.4)">
            Demo mock
          </Chip>
          <span data-testid="cli-demo-note" style={{ fontSize: 12.5, color: C.muted }}>
            — a simulated terminal, not the live CLI. Commands and versions shown here are canned.
          </span>
        </div>
      </div>
      <div style={{ borderRadius: 10, overflow: "hidden", maxWidth: 840, boxShadow: "0 12px 28px rgba(0,0,0,0.45)", border: "1px solid #26292e" }}>
        <div style={{ background: "#161B22", padding: "9px 14px", display: "flex", alignItems: "center", position: "relative" }}>
          <div style={{ display: "flex", gap: 8 }}>
            <Dot color="#ED6A5F" size={12} />
            <Dot color="#F6BE50" size={12} />
            <Dot color="#61C554" size={12} />
          </div>
          <span style={{ position: "absolute", left: 0, right: 0, textAlign: "center", fontSize: 12, color: "#8a8d91", pointerEvents: "none" }}>
            zsh — agentos
          </span>
        </div>
        <div
          ref={bodyRef}
          style={{ background: "#0D1117", padding: "14px 18px", fontFamily: C.mono, fontSize: 13, lineHeight: 1.6, color: "#c9d1d9", height: 360, overflowY: "auto" }}
        >
          {rows}
          <div style={{ display: "flex", alignItems: "center", whiteSpace: "pre", marginTop: 8 }}>
            <span style={{ color: C.brand, fontWeight: 700, flexShrink: 0 }}>❯ </span>
            <input
              ref={inputRef}
              value={input}
              autoFocus
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") run(input);
              }}
              placeholder="agentos …"
              style={{ flex: 1, background: "transparent", border: "none", outline: "none", color: "#e6edf3", fontFamily: C.mono, fontSize: 13, padding: 0 }}
            />
          </div>
        </div>
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 14, alignItems: "center", maxWidth: 840 }}>
        <span style={{ fontSize: 12, color: C.muted, fontFamily: C.mono }}>try:</span>
        {CHIPS.map((c, i) => (
          <button
            key={i}
            type="button"
            onClick={() => {
              if (c[1]) run(c[0]);
              else {
                setInput(c[0]);
                inputRef.current?.focus();
              }
            }}
            style={{
              fontFamily: C.mono,
              fontSize: 12,
              padding: "5px 10px",
              borderRadius: 7,
              cursor: "pointer",
              background: C.card,
              color: C.text2,
              border: "1px solid " + C.border,
              maxWidth: 340,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
            {...hoverBg(C.card, C.hover)}
          >
            {c[0].length > 34 ? c[0].slice(0, 33) + "…" : c[0]}
          </button>
        ))}
      </div>
    </div>
  );
}
