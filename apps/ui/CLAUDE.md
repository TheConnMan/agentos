# CLAUDE.md — apps/ui

The AgentOS console: Vite + React + TypeScript, no meta-framework. Owning
tasks: H1a (shell, fixture data), H1b (wired to the real backend), OB1
(Metrics/Logs wired to Langfuse). Full structure and wiring detail in
`apps/ui/README.md`.

## Load-bearing invariants

- **`AgentOS.html` at the repo root is the design canon.** Every view, token,
  and layout is ported from it verbatim (per `claude-design-prompt.md` §4:
  the exported source outranks the prompt). If a design question is
  ambiguous, open the canon and check its rendered state before improvising
  a new pattern -- do not invent new visual language.
- **The fixture-vs-wired gate is a runtime URL param, not a build flag.**
  `?state=N` (1-6) seeds a fixture level; `?api=1` (or `VITE_WIRED=1` at
  build time) flips wired paths on. A single build serves both the fixture
  demo and the live app -- do not fork the build per mode.
- **Not everything is wired yet.** Create-agent + Deploy, the Runs tab, and
  Metrics/Logs (OB1) call the real API. Fleet, Evals, Versions, Usage, and
  Cost are still fixtures. Before wiring a new view, check
  `apps/ui/README.md`'s "Wired / Still fixtures" split so you don't assume
  a backend endpoint exists that hasn't been built yet.
- **All API calls are same-origin `/api`, proxied by Vite.** `apps/api` has
  no CORS middleware on purpose -- the browser must reach it same-origin.
  `vite.config.ts` proxies `/api` to `AGENTOS_API_TARGET` and strips the
  prefix. Do not add a cross-origin fetch to `apps/api`; if a new surface
  genuinely needs CORS, that is an `apps/api` change to raise with its
  owner, not a workaround in the UI.
- **The OB1 Metrics tab intentionally diverges from the design canon.** The
  canon shows a PromQL query bar; the real API is Langfuse aggregates with
  no Prometheus, so the wired view keeps the card-grid/hero-chart layout but
  swaps the PromQL bar for an honest `langfuse {...}` descriptor. This is a
  deliberate, documented divergence (see `apps/ui/README.md`) -- do not "fix"
  it back toward the canon without checking why it diverged.
- **The demo-controls bar from the design is deliberately not shipped.**
  `?dev=1` gives an equivalent state switcher for development; keep it
  hidden without that param so the product view stays clean.

## Playwright ports (do not confuse these)

- `pnpm dev` -> `5173` (interactive dev server, hot reload).
- `pnpm preview` -> `4173` (production build preview).
- The **Playwright suite** builds and previews on its own dedicated port,
  **`4273`** (`PW_PORT`, see `playwright.config.ts`) -- deliberately distinct
  from both of the above so a stray dev/preview server never gets mistaken
  for the one under test. Two projects share one build: `chromium`
  (stackless, `e2e/*.spec.ts`, always runs) and `integration`
  (`e2e/integration/*.spec.ts`, needs the compose stack + `apps/api`, only
  runs when `PW_INTEGRATION=1`). Default `pnpm e2e` runs stackless only, so
  CI stays green without a backend.

## Verify

```bash
cd apps/ui
pnpm install
pnpm lint          # eslint, zero warnings allowed
pnpm typecheck     # tsc -b --noEmit
pnpm test          # vitest unit tests
pnpm e2e           # playwright, stackless (chromium project)
```

Integration E2E (needs the compose stack + a running `apps/api` with the
schema applied):
```bash
(cd ../api && uv run alembic upgrade head)
(cd ../api && uv run uvicorn agentos_api.main:app --port 8000) &
PW_INTEGRATION=1 AGENTOS_API_TARGET=http://localhost:8000 pnpm exec playwright test --project=integration
```
