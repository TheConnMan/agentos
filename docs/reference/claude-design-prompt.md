# Supabase for Agents: Claude Design Prompt

> **Historical document.** This is a pre-build reference/design document, preserved as engineering history. It is not living documentation and is not maintained. For the system that was actually built, read the root [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md); for forward-looking work read [`../roadmap.md`](../roadmap.md).

Big plan for the "Supabase for agents" prototype (the open-source developer platform for Slack-based agents named in the 2026-07-02 Claude Tag/Viktor discussion). Section 3 is the deliverable: a fully self-contained prompt to paste into Claude Design. Sections 1-2 are the context and decision record that shaped it.

## 1. Competitive verdict (why this design is worth building)

Full research ran 2026-07-02 across ~40 candidates with per-capability scoring (~90 fetched URLs). Headline: **no single shipping product does the full combination — but this is a fast-forming category, not an open field.** Not a non-starter; not greenfield either.

- **xpander.ai is the closest architectural analog** (do not lump it into the "AI teammate" bucket): developer-grade, self-hostable to K8s/VPC/on-prem/air-gapped, full Slack-native deploy and observability, and a real local-dev CLI (`xpander dev`) that routes live Slack traffic to your machine. Its gaps: no browser skill.md authoring, no git-flow bot identities, and evals-as-CI unconfirmed in its actual docs (marketing implies more than the docs show). MIT SDK, early-stage (~$3M raised).
- **Vercel eve is the nearest dev-workflow analog and best-resourced threat** (Apache-2.0, launched 2026-06-17): filesystem-first skills, `eve eval` as a CI deploy gate, OTel tracing, one-command Slack channel. Gaps: code-first (no browser authoring), explicitly cannot test the Slack surface locally (live workspace/ngrok required), production Vercel-hosted only (self-host roadmapped).
- **Slack Agent Kit**: Slack itself shipped the C1 rails plus local hot-reload in 2026. Build ON those rails, not a bespoke Slack layer.
- **Mastra Cloud** does zero-code Slack channels plus full self-host. eve, Mastra, and xpander all ship fast; assume partial gap-closure within 6-12 months.
- **LangGraph/LangSmith (Fleet + Deployment)** proves the platform mechanics are a solved category and is the only product with full browser authoring — but closed-source, Enterprise-priced, business-leaning. LangChain is one product-merge away.
- **OpenAI vacated the layer**: Agent Builder and Evals deprecate 2026-11-30; their migration docs point to the code-first Agents SDK and third-party Promptfoo.
- **Anthropic is the biggest structural watch item**: Claude Managed Agents (beta 2026-04, $0.08/session-hour) has version pinning, rollback, Console tracing, self-hosted sandboxes. A Slack connector plus eval gate from them collapses most differentiation. Counterpoint: Claude Tag proves they are keeping the Slack-teammate surface closed — the open/self-hostable/multi-runtime position is one no platform owner is taking. Watch their changelog.
- **The genuinely unoccupied ground** (verified across all research clusters): (a) git-flow with distinct version-tagged `@bot`/`@bot-dev` Slack identities — none anywhere; (b) a local CLI that *emulates* an inbound Slack message with no live workspace — unbuilt everywhere; (c) browser skill.md/plugin authoring with instant deploy — only closed LangSmith Fleet has it. **Evals-as-CI alone and Slack-deploy alone are table stakes** (8+ products each); the wedge is the combination, plus riding the Claude Code plugin ecosystem as the artifact format.
- Graveyard note: Julep ("Firebase for agents") and Castari ("Vercel for AI Agents") both died within a year as pure-managed plays; the OSS-core-plus-managed-cloud shape (Langfuse, Mastra, Agno) is what survives. Monetization, not product shape, is the risk.
- Naming caution: Supabase itself markets a "Supabase for Agents" solutions page (Postgres backend for agent workloads). Different category, same phrase; the product needs its own name. All third-party "X for agents" brandings found are dead or vaporware.

## 2. Design decisions record

Pinned by a maintainer in this session:

1. **One full design.** The console is the only product surface designed. GitHub is never rendered (the console's own Eval Runs view plus terminal `git push` output tells the CI story). Slack appears as exactly one small static thread card at the agent-live payoff moment (re-tinted once for the dev-bot story). The terminal flip stays.
2. **Neutral codename, Supabase-dark skin.** Default codename: **AgentOS** (bot handle `@agentos`, CLI `agentos`). Alternates if AgentOS collides: Dispatch, Agentbase. The name is a placeholder, deliberately not company-branded.
3. **The "complicated" demo state is a multi-agent fleet** (the AgentOS / leave-behind fleet-view story).
4. **Single self-contained HTML file** via Claude Design (an internal convention), not a React component.
5. **Maintenance is a headline act**, with all four heroes: promote-failure-to-eval, drift/regression timeline, usage analytics, cost per agent. Plus a maintainer's addition, which the design treats as the centerpiece of the eval surface: **the Eval Matrix — run an eval suite against different commit hashes/versions side by side** (regression bisection; extends to model arbitrage later).
6. **Demo controls follow the RS staffing-plan v0 convention**: an out-of-product controls bar driving a typed state machine, explicitly framed as review scaffolding.

Sources feeding the prompt: Supabase design tokens extracted from supabase/ui compiled CSS; Supabase onboarding and CLI-parity flows; Stripe test/live toggle and key-reveal patterns; Vercel bot-comment/two-URL patterns; Neon branch-table model; GitHub Primer check-state vocabulary and colors; terminal chrome/typewriter specs (traffic-light hexes, `steps()` animation). All verified via direct fetches 2026-07-02.

---

## 3. THE PROMPT (paste everything below into Claude Design)

# AgentOS: developer platform for Slack agents — clickable product prototype

Build a single, fully self-contained HTML file (inline CSS and vanilla JS only, no external dependencies, no network calls, no build step) that prototypes the entire product experience described below. Everything must be clickable end to end with all state held in local JS. Desktop-first, 1440px design target; content must not horizontally scroll the page.

## What AgentOS is

AgentOS is an open-source "Supabase for agents": a developer-grade platform for Slack-based AI agents. A developer logs in, connects Slack, writes a `skill.md` in the browser (or uploads a plugin: a bundle of skills plus MCP server configs), hits Deploy, and the agent is live in Slack seconds later. Observability appears automatically. Agents live in git: the `main` branch is the prod bot (`@agentos`), the `dev` branch is the dev bot (`@agentos-dev`), and every PR runs the agent's eval suite as a check. A local CLI (`agentos`) runs the same harness on the developer's machine and can emulate sending the bot a Slack message from the terminal. The differentiator vs "AI teammate" products: hard enforcement (deterministic skills, evals, observability), not "tell the bot to do better and hope."

The prototype's job is to sell three acts: (1) onboarding is magic, (2) the git-flow/CI story feels like home to any developer, (3) the maintenance surface (traces, evals, usage, cost) is where the platform earns its keep.

## Demo controls bar (review scaffolding, not product)

Fixed bar at the very top of the page, visually outside the product: amber dashed top-border, dark amber-tinted background, small mono label "DEMO CONTROLS — not part of product". It contains:

- Six state buttons driving a state machine typed as `'fresh' | 'slack-connected' | 'agent-live' | 'agent-ci' | 'plugin' | 'fleet'`, labeled: "1 Fresh account", "2 Slack connected", "3 First agent live", "4 Agent + CI", "5 Plugin installed", "6 Fleet". The active state button is highlighted. Clicking a state instantly re-renders the whole app to that state (all views, nav badges, data).
- A "Replay moment" button that re-triggers the current state's hero animation (e.g. the confetti or the typewriter) without changing state.
- The demo controls bar must never appear inside the terminal view or affect layout metrics of the product below it.

Wire all state transitions internally via plain JS state + render functions so the prototype is fully clickable without the demo bar too: a reviewer starting at state 1 can click through the real product flow and arrive at state 3 organically. The demo bar is the shortcut, not the only path.

## Design system (follow exactly; do not invent new colors, radii, or shadows)

Supabase-style dark developer console. Elevation is done with 1px border steps, never box-shadows (the only shadow allowed is under the fake terminal and the Slack card, listed below).

```
page/canvas        #121212      sidebar            #171717
darkest panel      #0f0f0f      card/surface       #1f1f1f
elevated/hover     #292929      input bg           #242424
selection          #313131      border default 1px #2e2e2e
border strong      #363636      border stronger    #454545
text primary       #fafafa      text secondary     #b4b4b4
text muted         #898989      text disabled      #4d4d4d
brand accent       #3ecf8e  (CTAs, active nav, logo, focus rings — use sparingly)
link green         #00c573      destructive        #e54d2e
success            #2EA043      failure            #CF222E
pending/warn       #BF8700      muted status       #8B949E
radii: 6-8px buttons/inputs, 12-16px cards
```

Typography: UI in a clean sans stack (`system-ui, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif`), body 14px/400, labels 14px/500, headings 24-32px/400 (not bold-heavy). All code, tokens, IDs, connection info, and the terminal in `"SF Mono", Menlo, Monaco, Consolas, "Liberation Mono", monospace` at 12-13px.

Microcopy register: terse, confident, developer-native. Possessive empty states ("Create your first agent"), no exclamation marks except the single celebration moment.

## Layout

Left sidebar (width ~220px, bg #171717): AgentOS logotype (wordmark + a small green dot), project switcher ("acme-corp / production"), then nav: Overview, Agents, Runs, Evals, Usage, Cost, Versions, Connections, Settings. Nav items show small count badges where the state warrants (e.g. Agents "3" in fleet state). Active item gets a #3ecf8e left rail and #292929 bg.

Top bar (within product, below the demo bar): breadcrumb, a persistent environment toggle, and the Terminal flip toggle (both specified below).

### Environment toggle (Stripe pattern)

A persistent pill toggle in the top bar: **PROD** (green #3ecf8e tint) / **DEV** (amber #BF8700 tint). When DEV is selected, a thin amber banner appears under the top bar across every view: "Viewing dev environment — @agentos-dev · branch dev". All data tables re-render with dev-tagged data. In states 1-3 the toggle is disabled with tooltip "Connect GitHub to get environments" (states 1-2) — it activates in state 4+.

### Terminal flip (the local-experience view)

A toggle in the top bar labeled with a terminal glyph and "CLI view". Flipping it replaces the current view's main content area with a fake macOS terminal showing the local-CLI equivalent of what the user just did in the UI — same outcome, local workflow. This is a core product story ("everything you do here works identically from your terminal"), not demo scaffolding.

Terminal chrome spec: rounded 10px window, title bar #161B22 with traffic-light dots (#ED6A5F, #F6BE50, #61C554, 12px), centered muted title (e.g. "zsh — agentos"), body bg #0D1117, `box-shadow: 0 12px 28px rgba(0,0,0,0.45)`, mono font, 13px, line-height 1.6. Prompt is a bold green `❯ `. Typed commands animate with a CSS `steps()` typewriter (~40-60ms/char feel) followed by output lines that fade in staggered (~80ms apart). Output coloring: #2EA043 success lines, #CF222E errors, #8B949E dim/info. A blinking block cursor ends the session.

Per-view terminal content (write these exact sessions, adjusted to state):

- Overview/onboarding views → `agentos init`, then `agentos start`, which prints a Supabase-style boxed summary:
  ```
  ╭─ agentos dev environment ──────────────────────────╮
  │  Local bot        http://localhost:7245          │
  │  Slack emulator   agentos send "<message>"         │
  │  Eval runner      agentos eval                     │
  │  Version          dev @ 4f2c91a                  │
  ╰──────────────────────────────────────────────────╯
  ```
- Agent editor view → `$EDITOR skills/deal-desk/skill.md`, then `agentos dev` (hot reload line), then the emulation moment: `agentos send "@agentos can we approve the Meridian deal at 18% discount?"` followed by the bot's streamed reply and a dim trace line `→ trace tr_8f3k21 · 3 tool calls · 2.1s · $0.04`.
- Evals view → `agentos eval` with per-case lines (`✓ approver-from-policy-source 1.2s`, `✗ deal-data-from-crm-not-slack 0.9s`) and a summary `34/36 passed`.
- Versions view → `git push origin dev` with dim git output ending in `→ agentos: eval check queued on PR #42` — this line is the only place GitHub exists in the prototype, as text.

## The six demo states and their story

### State 1 — `fresh` (signed up, nothing connected)
Overview shows a setup checklist card (Stripe go-live pattern): flat checkboxes — "Connect Slack", "Create your first agent", "Send it a message", "Connect GitHub for CI evals" — none checked, first item has the primary CTA. Every other nav view shows a teaching empty state (one sentence + one CTA + a muted "or explore with a demo agent" text link that jumps the demo to state 3 with a "Demo" badge on the data). Nothing feels broken; everything feels ready.

### State 2 — `slack-connected` (Slack wired, no agent)
The Connect Slack flow: clicking "Connect Slack" opens a modal styled like an OAuth grant (Slack-ish but generic: workspace "acme-corp.slack.com", permission list, "Allow" button). Pretend-through-buttons: clicking Allow closes the modal, checklist item 1 checks with a brief green tick animation, and Connections view now shows a connected row: green dot, "Slack · acme-corp.slack.com", mono workspace ID, "Connected 2 min ago". Checklist advances focus to "Create your first agent". Agents view empty state now says "Slack is connected. Create your first agent to put it to work."

### State 3 — `agent-live` (the magic moment)
The create-agent flow: "New agent" opens a single form (no wizard): name prefilled "deal-desk" (select-all-on-focus), a template picker grid (cards: "Deal desk approvals", "SRE triage", "Analytics Q&A", "Blank skill.md"), and a browser code editor pane (mono, dark, line numbers) pre-populated with a realistic ~25-line skill.md for deal-desk (frontmatter: name, description, tools: [salesforce-mcp, slack]; sections: "When to run", "Policy", "Hard rules" including "Approver names come ONLY from policy.yaml — never invent one" and "Deal amounts come from the CRM record, never from the Slack message"). One primary button: **Deploy**.

Clicking Deploy: button becomes a 700ms skeleton shimmer, then the hero moment — a compact confetti burst (canvas, one-shot, gated to first deploy only) and a success panel: "**@agentos is live in #revenue-ops** — replied to its first ping in 42ms". Below it, the single Slack evidence card (spec below) shows the proof. The checklist checks items 2 and 3. Runs view now shows the first trace. Observability exists without any setup step — that is the point; never show an "instrumentation" task.

**Slack evidence card spec** (the only Slack rendering in the entire prototype): a window-chrome card (same traffic-light treatment as the terminal, title "Slack — #revenue-ops"), bg #1a1d21, ~380px wide, three static messages: a human ("mara") asking "@agentos can we approve the Meridian deal at 18% discount?", the bot reply (green "APP" badge next to "@agentos") with a short structured answer ("Meridian Corp · $84,000 · 18% requested · **Needs approval** — policy caps auto-approve at 15%. Routed to approver from policy: J. Whitfield."), and a dim system line "only visible to you: trace tr_8f3k21". Non-interactive except one canned affordance: a small "Send test message" button under the card replays the exchange with a typing indicator. In state 4+, a variant of this card can render once with an amber "@agentos-dev" APP badge to show the dev-bot identity. Do NOT build any other Slack chrome: no workspace sidebar, no composer, no emoji.

### State 4 — `agent-ci` (git flow + evals as checks)
Connections gains GitHub (repo "acme/agentos-agents", connected row). Environment toggle activates. Versions view becomes real: a flat table (Neon pattern) with columns Branch, Version, Deployed, Eval score, Created by (mix of human avatars and "agentos-ci" automation rows), Status — rows for `main @ v1.4.2` (prod, green) and `dev @ 4f2c91a` (amber). Evals view fills: the suite "deal-desk core" with 36 cases listed by name and per-case pass/fail chips, GitHub-vocabulary summary line ("34 of 36 checks passed" style, but styled with AgentOS tokens, not GitHub's UI), durations per case.

Two hero flows here:

1. **Promote failure to eval.** In Runs, one trace is marked failed (amber-red left rail): drilling in shows the timeline (Slack message → skill invoked → tool call `salesforce.get_deal` → response) with the failure annotated ("approver 'Dana' not found in policy.yaml — hallucinated value"). A primary button "Add as eval case" — clicking it shows a small inline form (case name prefilled "approver-from-policy-source"), then a toast "Eval suite: 36 → 37 cases" and the Evals view count updates. The compounding loop made visible in two clicks.
2. **Eval Matrix (the centerpiece).** Inside Evals, a tab "Matrix": pick suite "deal-desk core", pick versions via chips (`main@v1.4.2`, `dev@4f2c91a`, `dev@b7e02d1`, plus one model-swap chip "dev@4f2c91a · claude-haiku"), hit Run. Render a results grid: rows = eval cases, columns = versions, cells = compact pass/fail dots with a per-column aggregate score header (e.g. 97% / 94% / 86% / 91%), worst column tinted red, best column ringed green. One row shows the regression pattern (passes on v1.4.2, fails on both dev versions) and a footer line names it: "2 regressions introduced after 4f2c91a — likely cause: skill.md change in commit b7e02d1". This screen is the product's "aha" for developers; give it room.

### State 5 — `plugin` (upload path)
Agents view gains an "Install plugin" button opening a drag-drop dropzone modal (dashed border, file icon; accept ".zip or a git URL"). Pretend-upload "sre-triage-plugin.zip" shows a manifest preview (3 skills, 2 MCP servers: datadog, pagerduty; permissions list) and an Install button. Installing adds a second agent card "sre-triage" with a "Plugin" badge and its own eval suite (12 cases) — demonstrating that a plugin is the same first-class citizen as a hand-written skill.md.

### State 6 — `fleet` (the complicated thing)
Overview becomes a fleet dashboard: a table of 5 agents (deal-desk, sre-triage, rev-analytics, onboarding-faq, contract-review) with columns: Agent, Channel(s), Version (prod/dev chips), Eval score sparkline-ish trend (tiny inline SVG), Runs today, Cost today, Health (green/amber/red dot). One agent (rev-analytics) is degrading: amber health, eval trend dipping. Clicking it lands on its detail with the **drift timeline**: an SVG line chart of eval pass-rate over 30 days with version-deploy markers as vertical ticks; the dip visibly starts at marker "v2.1.0 · model update" — hovering a marker shows a tooltip naming the change. A "Compare in Matrix" button deep-links to the Eval Matrix pre-loaded with the two versions spanning the dip. Usage view (fleet-wide): who talks to which agent (bar list of top users), top intents ("approve deal", "why did checkout error spike", "MRR by segment"), escalation/override rate per agent ("humans overrode 4% of deal-desk verdicts this week" with a link to those runs). Cost view: per-agent per-version spend, cost per interaction, a callout "rev-analytics: identical eval score on smaller model would save $310/mo" (the model-arbitrage tease, fed by the Matrix's model column).

## Data realism rules

Realistic placeholder data everywhere; never lorem ipsum, never foo/bar. Company: acme-corp. Humans: mara, jt, priya, sam. Deals: Meridian Corp $84,000; Northwind $23,500. Eval case names read like real guardrails: `approver-from-policy-source`, `deal-data-from-crm-not-slack`, `no-discount-above-policy-cap`, `escalates-ambiguous-terms`. Traces have ids like `tr_8f3k21`, real-looking durations (0.4-6s), token counts, and costs ($0.01-$0.12). Timestamps are relative ("2 min ago", "Tue 14:02").

## Interaction and quality bar

- Every screen reachable from the sidebar in every state (empty states where the state warrants).
- All transitions instant except the deliberately-staged ones: 700ms deploy skeleton, typewriter in terminal view, confetti (once), toast notifications (2.5s).
- Hover states on all rows/buttons (#292929). Focus-visible rings in #3ecf8e.
- Copy-to-clipboard affordances (icon + "Copied" toast) on: workspace ID, bot token (shown Stripe-style: `rly_secret_...` masked with a one-shot "Reveal" toggle), trace ids, CLI commands.
- No routing library, no frameworks: a single `render(state)` approach with template strings or DOM building is fine. Keep the code organized enough to find each view's builder.

## Prioritized deliverables

If you must trim, nail these in order — they define the product: (1) State 3's create-agent → Deploy → live-in-Slack moment with the Slack evidence card; (2) the Eval Matrix; (3) the Terminal flip with the `agentos send` emulation session; (4) State 6's fleet dashboard with the drift timeline; (5) the demo controls bar itself. Settings and Connections can be minimal; do not build billing, auth screens, team management, or docs pages at all.

## Stop when

You have one self-contained HTML file where all six demo states render, the five prioritized moments above are clickable and animated, and a reviewer can walk states 1→6 from the demo bar or organically via the product CTAs. Stop after the naive correct implementation. Do not add abstractions, extra views, real network calls, or flexibility not requested.

---

## 4. After the design generates

- Per the mock-source learning: once Claude Design produces the artifact, **the exported source is the design canon, not this prompt**. Read the generated file before planning any port; expect it to contain component shapes and micro-decisions this prompt never named.
- Codename check: "AgentOS" collides with agentos.app (workflow tool) — fine for a prototype; revisit before anything public. Alternates: Dispatch, Agentbase.
- Standing watch items from the research: Vercel eve changelog (weekly releases), Mastra Channels, xpander.ai (closest analog; watch for browser authoring or CI evals landing), and Anthropic's Managed Agents changelog for "Slack" or "evals" additions. One known research gap: Salesforce Agentforce-in-Slack was not deep-researched this pass.

## 5. Research provenance

Competitive scan and DX research ran 2026-07-02 in this session via parallel research agents; every product claim traces to a fetched URL (capability tables delivered in-session). Key fetched sources: github.com/vercel/eve, vercel.com/blog/introducing-eve, vercel.com/kb/guide/eve-slack-agent-starter, mastra.ai/blog/introducing-channels, docs.langchain.com/langsmith/deployment, developers.openai.com/api/docs/deprecations, platform.claude.com/docs/en/managed-agents/overview, claude.com/blog/claude-managed-agents, supabase/ui dark.css (design tokens), supabase.com/docs/guides/local-development/cli/getting-started, docs.stripe.com/keys, docs.stripe.com/test-mode, vercel.com/docs/git/vercel-for-github, neon.com/docs/manage/branches, docs.github.com (status checks, workflow commands), primer.style/octicons. Durable findings were captured separately.
