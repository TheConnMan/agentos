# apps/ui

The AgentOS console: Vite + React + TypeScript, no meta-framework. Design tokens
and every view are ported from the design canon (`AgentOS.html` at the repo
root). This is task **H1a**: the shell running entirely on fixture data. **H1b**
wires it to the real backend (B1/B2); **OB1** replaces the fixture Metrics/Logs
with live data.

## Verify

Run from `apps/ui` (dependencies: `pnpm install` once):

```bash
pnpm build          # tsc project build + vite production build
pnpm lint           # eslint, zero warnings allowed
pnpm test           # vitest unit tests (reducer, fixtures, component smoke)
pnpm exec playwright test   # E2E, headless; builds + previews automatically
```

`pnpm dev` serves the app on http://localhost:5173. `pnpm preview` serves the
production build on http://localhost:4173 (what Playwright drives).

## Fixture-state mechanism

There is no backend yet, and the design's demo-controls bar is deliberately not
shipped. Instead the six design states are driven by a URL param:

- `?state=N` (N = 1..6) loads the app at that fixture level on mount. 1 fresh,
  2 slack-connected, 3 agent-live, 4 agent-ci, 5 plugin, 6 fleet.
- `?dev=1` additionally shows a small bottom-left state switcher for manual
  clicking during development. It is hidden without the param, so the product
  view stays clean. Playwright uses `?state=N` directly.

The whole app is otherwise clickable end to end: starting at `?state=1` a user
can Connect Slack, create an agent, deploy, and reach the live state organically.

## Structure

- `src/tokens.ts` — the `C` design-token block, verbatim from the canon.
- `src/state/` — the app state machine (`store.tsx` reducer + `useStore` hook)
  and its types. `initialState(level)` seeds a fixture level.
- `src/fixtures/` — the seeded data model (agents, traces, evals, versions,
  logs, metrics). Trace spans are typed against the frozen ACI contract
  (`@aci/aci-protocol`, aliased to `packages/aci-protocol/generated/ts`).
- `src/primitives/` — hand-rolled design-system primitives (Button, Card, Chip,
  Dot, StatusDot, Tabs, Modal, Toast, Table, EmptyState, SectionTitle,
  Sparkline, AreaChart, CopyButton). No component library.
- `src/components/` — app chrome (Sidebar, Topbar, Confetti, SlackCard, the
  state switcher) and the modals (`modals/`).
- `src/views/` — one file per view: Overview (+ fleet + success panel), Agents,
  AgentDetail (drift), Evals (`evals/Suite`, `evals/Matrix`), Observability
  (`obs/*`: Traces, Metrics, Logs, MemoryStub, Usage, Cost), Versions,
  Connections, Settings, Terminal (the CLI REPL).
- `e2e/` — Playwright specs. `design-review/` — committed side-by-side fidelity
  screenshots (impl vs the design canon).

## For H1b (wiring the backend)

Every screen reads from `src/fixtures` and the fixture-level selectors. To wire
real data, replace the fixture calls with API reads without touching the views'
markup:

- Data entry points are the `*ForLevel` / seeded exports in `src/fixtures`
  (`agentsForLevel`, `tracesForLevel`, `traceSpans`, `EVAL_CASES`,
  `VERSION_ROWS`, `logsForLevel`, the metric series). Swap these for API-backed
  hooks/loaders.
- Mutating actions are all funneled through the store reducer
  (`src/state/store.tsx`): `deployDone`, `allowSlack`, `installPlugin`,
  `promoteEval`, `connectGitHub`, etc. These are where create/deploy/promote
  calls hit B1/B2; the reducer already models the resulting UI state.
- The ACI contract types are already imported for trace spans; extend that
  typing to real trace payloads from the Langfuse proxy.

## Scope notes (H1a decisions)

- Onboarding uses the classic create-agent modal (template picker + skill.md
  editor + Deploy), not the interview wizard (deferred).
- The Observability Memory tab is a designed coming-soon stub; the full memory
  browser is deferred. The ACI `memory_ref` seam stays in the contract.
- Metrics and Logs are scaffolded on deterministic fixture data matching the
  design; OB1 wires them to Langfuse.
