# apps/ui

The AgentOS console: Vite + React + TypeScript, no meta-framework. Design tokens
and every view are ported from the design canon (the original design mockup).
This is the UI shell. It runs entirely on fixture data first, then is wired to
the real backend; the Metrics/Logs tabs move from fixtures to live
observability endpoints.

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

## Backend wiring

Several paths now run against the real API; everything else stays on fixtures.

**Wired (real API):**
- Create agent + Deploy: the create-agent modal POSTs `/agents` and
  `/agents/{id}/versions`, packages the editor's skill.md into a plugin bundle
  client-side (jszip), and PUTs it to `/agents/{id}/versions/{vid}/bundle`. The
  bundle validator's 422 `errors[]` render inline under the editor.
- Runs tab (Observability > Traces): reads `GET /langfuse/traces` and
  `/langfuse/traces/{id}` through the proxy and renders the real span tree.
- Metrics tab (Observability > Metrics): summary stat cards from
  `GET /observability/metrics/summary` plus a selectable time-series chart from
  `/observability/metrics/series` (metric x granularity). Langfuse-backed, five
  metrics (runs, latency p95, tokens, cost, error rate). See the divergence note
  below.
- Logs tab (Observability > Logs): the runner-pod log viewer over
  `GET /observability/runners/{namespace}/{pod}/logs`, with designed distinct
  states for 503 (no cluster), 404 (pod not found), and 502 (other cluster
  error).
- Cost tab (Observability > Cost): agent-scoped (an agent selector, since
  the cost endpoints are per agent). Total spend + daily chart from
  `GET /agents/{id}/cost` (honest empty state when the series is all zero, e.g. a
  fresh agent); budget display + edit against `GET/PUT /agents/{id}/budget`
  (client-side positive-number validation, server 422 surfaced inline); and the
  kill switch over `GET/POST /agents/{id}/kill` + `POST /agents/{id}/resume` --
  an emergency stop with confirm-before-kill and an unmistakable red killed-state
  banner. The wired Cost view is per-agent panels rather than the fixture fleet
  table (the API is per agent).

**Metrics divergence (deliberate).** The design canon's Metrics tab is a
Prometheus/PromQL surface (a `rate(...)` query bar, a request-rate hero, and
p50/tool-calls/active-sessions panels). The real API is Langfuse aggregates over
five metrics with no Prometheus, so the wired view keeps the card-grid +
hero-chart layout but drops the PromQL bar (replaced by an honest `langfuse {...}`
descriptor) and shows only the five API-backed metrics. The per-agent filter is a
trace-name substring server-side, so it is presented as a plain "name contains"
filter, not exact matching.

**Still fixtures:** fleet, evals, versions, usage, and states 4-6 for the
non-wired demo. The fixture Metrics/Logs/Cost (the full canon design) still
render without `?api=1`. Swap the rest by replacing the `src/fixtures` selectors.

**How wiring is gated.** `src/api/config.ts` resolves the mode at runtime, so a
single build serves both the fixture demo and the live run:
- Wired ON when the URL has `?api=1` (or the build set `VITE_WIRED=1`).
- There is no API key in the browser (#630 / [ADR-0049](../../docs/adr/0049-console-sessions-and-cli-minted-login-codes.md)).
  A wired console authenticates with an HttpOnly session cookie, exchanged for a
  single-use login code minted by `agentos <local|cluster> console login`. Every
  call sends `credentials: "same-origin"` and no `X-API-Key` header. `?api_key=`,
  `VITE_API_KEY`, and the dev-key fallback are deleted, not deprecated.
- `ConsoleGate` (`src/components/ConsoleGate.tsx`) sits above the shell: while
  unauthenticated it renders the login view *instead of* the console, so a locked
  console mounts no provider and makes no authenticated call. Fixture mode never
  calls the session endpoint at all.
- All calls go to the same-origin `/api` prefix. `vite.config.ts` proxies `/api`
  to `AGENTOS_API_TARGET` (default `http://localhost:8000`), stripping the
  prefix. This avoids CORS: apps/api has no CORS middleware, so the browser must
  reach it same-origin.

**Client layer.** `src/api/`: `client.ts` (typed calls + `BundleValidationError`,
the observability calls `getMetricsSummary`/`getMetricSeries`/`getRunnerLogs`,
and the cost calls `getCost`/`getBudget`/`putBudget`/`getKillState`/`killAgent`/
`resumeAgent`/`getAgents`), `bundle.ts` (jszip packaging + the testable
`bundleFileTree`), `hooks.ts` (`useTraces`/`useTrace`/`useMetricsSummary`/
`useMetricSeries`/`useAgents`/`useCost`), `config.ts` (the wiring gate). Deploy
failures flow through the store reducer actions `deployFailedValidation` /
`deployFailed`. Wired Observability lives in `src/views/obs/Real*.tsx`
(`RealTraces`, `RealMetrics`, `RealLogs`, `RealCost`), branched from the fixtures
in `Observability.tsx` by `isWired()`.

**Integration E2E (needs the live stack).** With the compose dev stack up and
apps/api on 8000:

```bash
# apply the schema once
(cd ../api && uv run alembic upgrade head)
# run uvicorn in another shell: (cd ../api && uv run uvicorn agentos_api.main:app --port 8000)
PW_INTEGRATION=1 AGENTOS_API_TARGET=http://localhost:8000 pnpm exec playwright test --project=integration
```

The integration spec (`e2e/integration/`) seeds a real OTLP trace
(`seed_trace.py`, run via `uv run`), drives create -> Deploy -> verifies the
version + stored bundle via the API, checks the malformed-skill.md validator
error inline, and asserts the Runs list + span-tree drill-in. The default
`pnpm exec playwright test` excludes it, so stackless CI stays green.

The wired Metrics/Logs are covered in the **stackless** suite
(`e2e/observability-wired.spec.ts`) by running the app in `?api=1` mode and
stubbing the observability endpoints with real-shaped responses via Playwright
route interception (200 metrics, 503 no-cluster logs, 200 logs) — no backend
needed. Against a real API on the dev box, runner-logs returns 502 rather than
503 because a kubeconfig is present (the 401-rejecting cluster), which the UI
renders as the "Cluster error" state.

Config reference: `.env.example`.

## Wiring the rest

- Remaining data entry points are the `*ForLevel` / seeded exports in
  `src/fixtures` (`agentsForLevel`, `EVAL_CASES`, `VERSION_ROWS`, `logsForLevel`,
  the metric series). Swap these for API-backed loaders.
- The ACI contract types are imported for the fixture trace spans; the wired
  Runs path renders the API's `ObservationNode` tree directly.

## Scope notes

- Onboarding uses the classic create-agent modal (template picker + skill.md
  editor + Deploy), not the interview wizard (deferred).
- The Observability Memory tab is a designed coming-soon stub; the full memory
  browser is deferred. The ACI `memory_ref` seam stays in the contract.
- Metrics and Logs are scaffolded on deterministic fixture data matching the
  design; the observability endpoints wire them to Langfuse.
