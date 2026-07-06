# Relay (AgentOS) MVP: Build Orchestration Plan

> **As-built status: built.** The task DAG below (R0 through O1) was executed as
> a fleet of individually-verified lanes; 32+ merged. The spine (R0, C1, D1, G1,
> F1) plus Waves 1-4 landed, including the eval plane (K1), budgets (L1), and
> observability (OB1); N1 (soak) remains a scaffold and SK's formal recorded run
> log is the outstanding cold-start rehearsal. This doc is the record of how the
> build was orchestrated. For the resulting system read the root
> [`../ARCHITECTURE.md`](../ARCHITECTURE.md); for forward work read
> [`roadmap.md`](roadmap.md).

How the v0.1 MVP gets built as a fleet of individually-verifiable background jobs: the task list with ordering and parallelization, the success criteria per task, the self-verification harness that lets each job prove its own work, and the dispatch/merge workflow. Companion to `mvp-build-plan.md` (the architecture spine) and `reference/detailed-architecture.md` §11 (the task DAG this refines).

Author: 2026-07-05, after reviewing the design canon against the build docs and auditing the verification substrate (k8scratch, toolchains, Playwright).

## 1. What the design file adds to the scope

The design canon (per `claude-design-prompt.md` §4: the exported source outranks the prompt) is authoritative. Its feature index enumerates 18 shippable features in 6 groups plus 2 scaffolding items. Mapping against the MVP scope in `mvp-build-plan.md` §4 surfaced three deltas; Brian decided all three on 2026-07-05:

| Design feature | MVP docs say | Decision |
|---|---|---|
| **Conversational onboarding** (interview wizard drafts the agent; it IS the design's onboarding, feature 01) | Interview Me compiler is v1.1, explicitly out | **DEFERRED.** MVP onboarding uses the classic create-agent modal (template picker + skill.md editor + Deploy) that the design already contains at `newAgentModal()`. The interview wizard waits for v1.1's real compiler. |
| **Agent memory tab** (inspect / edit / forget memories, with trace-linked sources) | Automatic memory *generation* is v1.1 | **DEFERRED.** The tab renders an empty/coming-soon state. The ACI's `AGENTOS_MEMORY_REF` seam stays in the contract so nothing blocks the v1.1 build. |
| **Metrics + Logs tabs** (Prometheus-style charts, Loki-style tail) | Not in the six done-when behaviors | **IN MVP, Langfuse-backed (decided 2026-07-05).** Every series the design's Metrics tab shows (runs, latency, tokens, cost, error rate per agent over time) is derivable from Langfuse's Metrics API — no Prometheus adoption needed for the product surface. The Logs gap is covered by a per-run "runner logs" affordance proxying the K8s pod-logs API for the sandbox that served the trace. Full Loki live-tail + kube-prometheus-stack adoption is deferred to the fleet/leave-behind phase; our services still expose `/metrics` endpoints so an operator's existing Prometheus can scrape them. New task OB1 (§4). |

Everything else in the feature index maps cleanly onto the existing DAG: Overview/Fleet → H1/state-6, Agents list + create + plugin install → H1+B2, Traces & span detail → H1 Runs view (proven on the Langfuse API), Evals & matrix → K1, Versions & promotion → J1, PROD/DEV env toggle → J1, Connections → B1/E1/J1, Settings & API keys → B1, CLI terminal view → I1.

Naming: repo and commits say **Relay**; the design and CLI say **agentos**. Treat "Relay" as the project codename and keep `agentos` as the product-surface name (bot handle, CLI binary) until a naming decision; nothing in the build hinges on it.

## 2. Stack decisions (locked this session)

Brian's calls, replacing `detailed-architecture.md` §9's "TypeScript end-to-end" lean:

- **Backend: Python 3.13** — API server (**FastAPI**), dispatcher (**Slack Bolt for Python**, Socket Mode), worker (plain **redis-py against Valkey Streams** with consumer groups — the concurrency kernel is hand-rolled correctness logic either way; a framework like Celery/arq would obscure the finish-race CAS, and Streams give explicit ack + at-least-once + idempotency keys), runner (**claude-agent-sdk Python** — this is what the prototypes proved, so the proven code seeds production directly). Poetry or uv workspace, pytest, ruff, mypy.
- **UI: React** — Vite + React + TypeScript, no meta-framework. Design tokens lifted verbatim from the design canon's `C = {...}` block. Playwright for E2E.
- **CLI: Rust** — clap + tokio + reqwest. The CLI is a good Rust fit: it's a standalone binary (the whole point is `agentos init/start/send/eval` on a dev laptop with nothing installed), it only speaks the frozen contracts over HTTP/NDJSON, and it's off the critical path so the slower iteration loop costs nothing. `agentos start` orchestrates the local runner container via Docker.
- **The contract package (C1) becomes cross-language.** `packages/aci-protocol` and `packages/plugin-format` are authored as **Pydantic models (source of truth) → exported JSON Schema (committed) → generated TypeScript types (UI) and Rust types via typify (CLI)**. CI regenerates and diffs: a schema change that isn't backwards-compatible fails the build. This *strengthens* ADR-0005's frozen-ACI discipline — the versioned contract is now enforced across three languages by codegen, not convention.

## 3. The self-verification harness (build this first)

The user requirement: every job must verify its own work with no human in the loop. Four tiers, each wired into the repo before the tasks that need it:

**V0 — per-package tests + CI.** pytest (backend), vitest + Playwright (UI), cargo test (CLI). GitHub Actions runs all suites + lint on every PR. Integration tests run against the real dev stack (V1), never mocks of Postgres/Valkey/Langfuse (per §9 testing rules).

**V1 — the dev stack: docker compose.** One `compose.dev.yaml` bringing up Postgres + Valkey + Langfuse (+ ClickHouse pinned `:24.8` for AVX-less hosts) + MinIO + OTel Collector locally. This is the PT-4 setup productized (~2.3 GB, fits the dev box). Every backend integration test and every UI E2E runs against it. Also the seed of A1's Helm dev profile — same components, same pinned versions.

**V2 — UI verification: Playwright, two modes.**
- *The gate:* a committed Playwright E2E suite asserting behavior (deploy flow completes, runs view renders the tool-call tree, eval matrix grid populates) — runs headless in CI against the compose stack. This is the deletion-test-passing regression net.
- *The agent's eyes:* **`@playwright/mcp` added to this repo's `.mcp.json`.** Implementing agents drive the real browser interactively — click through the flow they just built, take screenshots, and diff against the corresponding design-canon demo state (the design mockup renders locally, so the agent can screenshot design-vs-implementation side by side). The MCP is for verification-during-development and visual fidelity checks; the committed test suite is the merge gate. Both use the Chrome + Playwright 1.61 already on this machine.

**V3 — cluster verification: k8scratch.** The scratch k3s cluster is alive, clean (prototype resources torn down), and reachable via the repo-local kubeconfig (`~/k8scratch/.kube/k8scratch.yaml`). It's where G1 (sandbox substrate), A1/A2 (Helm + security rails), and N1 (soak) verify. **Constraint: the box is 4 GB / 4 cores; the full-stack definition-of-done target is 8-10 vCPU / ~20 GB.** Plan: early cluster tasks (G1 sandbox lifecycle, A2 NetworkPolicy probes) fit in 4 GB with the observability stack left on the dev box; before the walking-skeleton integration gate, **resize the LXC to ≥8 vCPU / 16-20 GB** (Brian action, listed in §7). Fallback if resize is inconvenient: a `kind` cluster on the dev box for chart tests, with k8scratch reserved for sandbox/gVisor work — but the resize is strictly better because a resized k8scratch *is* the definition-of-done environment.

**External dependencies with no self-serve verification path (flagged, not blocking early waves):**
- *Slack:* the architecture is deliberately emulation-first — `agentos send` injects synthetic events with no workspace, so D1/F1/I1/most of E1 verify Slack-free. A real test workspace + **two** Slack apps (`@agentos`, `@agentos-dev`) is needed only at the walking-skeleton gate and for J1. Brian creates these (~15 min).
- *Anthropic credential:* dev runs use the `CLAUDE_CODE_OAUTH_TOKEN` path PT-2 proved. Runner integration tests that burn real tokens are tagged `@live` and run at integration gates, not on every PR.
- *GitHub App (J1):* needs the repo to exist on GitHub (see §7) and an App registration at Phase-3 time.

The harness itself is encoded in the repo's `CLAUDE.md` (verification commands per package, compose stack usage, k8scratch access pattern, Playwright MCP conventions, the frozen-contract escalation rule) — so every background job inherits the how-to-verify knowledge without re-discovery.

## 4. The ordered task list

The DAG from `detailed-architecture.md` §11, adjusted for the stack decisions, the design deltas (§1), and one new task each for repo bootstrap and the verification harness. Per task: what it owns, hard dependencies, done-when (the AC that goes verbatim into its ticket), and which verification tier proves it.

**The spine (sequential, cannot be parallelized):** `R0 → C1 → D1 → G1 → F1 → SK gate → N1`. Everything else fans out around it.

### Wave 0 — Foundation (sequential, ~2 jobs)

| ID | Task | Owns | Deps | Done when | Verify |
|---|---|---|---|---|---|
| **R0** | Repo bootstrap + verification harness: GitHub remote, monorepo layout (`apps/{api,dispatcher,worker,ui}`, `cli/`, `runner/`, `packages/{aci-protocol,plugin-format}`, `charts/agentos`), `compose.dev.yaml` (V1 stack), GitHub Actions CI skeleton, `.mcp.json` (+ `@playwright/mcp`), repo `CLAUDE.md` with per-package verify commands + k8scratch access + escalation rules | repo root | — | `docker compose -f compose.dev.yaml up` is healthy (Langfuse UI answers, PG/Valkey/MinIO up); CI runs green on a trivial PR; `CLAUDE.md` documents every verify command | V0+V1 |
| **C1** | Frozen contracts: `aci-protocol` (session protocol + NDJSON events as Pydantic, per detailed-architecture §0) + `plugin-format` (Claude Code plugin shape verbatim) + JSON Schema export + TS/Rust codegen + schema-compat CI test | `packages/*` | R0 | Pydantic models express the §0 contract; committed JSON Schemas regenerate byte-identical in CI; generated TS + Rust compile; a deliberately-breaking schema change fails the compat test | V0 |

### Wave 1 — Fan-out (up to 5 parallel jobs after C1)

| ID | Task | Owns | Deps | Done when | Verify |
|---|---|---|---|---|---|
| **A1** | Helm umbrella v0: Langfuse+PG+Valkey+OTel deps, dev profile, BYO toggles, the two preflights (CPU-AVX/ClickHouse pin, NetworkPolicy-enforcement probe) | `charts/agentos` | C1 | `helm install` on k8scratch (or kind) brings up the stack; both preflights demonstrably fire on a violating config | V3 |
| **B1** | API server core: FastAPI + Postgres schema (agents/versions/deployments CRUD, auth, Langfuse proxy endpoints), OpenAPI emitted | `apps/api` | C1 | CRUD integration tests pass against real Postgres from the compose stack; OpenAPI spec committed; Langfuse proxy returns a reconstructed trace tree for a seeded trace | V0+V1 |
| **D1** | Runner image + SDK adapter: productize the PT-2/PT-E prototype — streaming session server implementing full ACI v0.1, `AGENTOS_BUDGET` enforcement, `side_effect_flag`, plugin-bundle loading | `runner/` | C1 | ACI conformance suite (lives in `packages/aci-protocol`) passes: initial event + mid-run steer + interrupt + NDJSON stream + gen_ai spans in compose-stack Langfuse; budget ceiling halts a run; `@live` test proves real cache reuse | V0+V1 |
| **E1** | Dispatcher: Bolt-python Socket Mode, <3s ack + placeholder, enqueue to Valkey Streams with idempotency key, reconnect supervision | `apps/dispatcher` | C1 | Simulated Slack event (Bolt test harness) → acked, placeholder posted, queued exactly once; duplicate retry deduped; no real workspace needed | V0+V1 |
| **H1a** | UI shell + design-system port: Vite app, tokens/layout/nav from the design canon, all views scaffolded against fixture data matching the design's data model (Agent, Trace, EvalCase, Version, Memory), Playwright suite skeleton | `apps/ui` | C1 | Nav + all seven views render pixel-faithful to the design's states 1-3 (Playwright screenshot diff vs design-canon renders); zero backend calls yet | V0+V2 |

### Wave 2 — Integration lanes (parallel after their deps)

| ID | Task | Owns | Deps | Done when | Verify |
|---|---|---|---|---|---|
| **G1** | Agent Sandbox substrate: controller + extensions install in chart, warm pool, `thread_ts → sandbox_id` affinity store, claim/release, cold-restart rehydrate path design | `charts/agentos/agent-sandbox` + `apps/worker` (substrate module only) | A1, D1 | On k8scratch: warm-pool claim <1s binds a pre-warmed sandbox running the D1 image; consecutive turns to the same claim show `cache_read_input_tokens > 0`; suspend/resume rehydrates from history (asserted, not assumed) | V3 |
| **B2** | Plugin bundle pipeline: validate (via `plugin-format`) → immutable versioned bundle → MinIO/S3, fetch-by-version | `apps/api` (bundles module) | B1 | Upload → validated → versioned bundle stored; malformed bundle rejected with actionable error; runner fetches by version in an integration test | V0+V1 |
| **H1b** | UI wired to real backend: editor→deploy against B1/B2, Runs view on the Langfuse proxy, states 1-3 real | `apps/ui` | B1, B2, H1a | The design's state-3 flow (create → Deploy → first trace visible) works E2E against the compose stack; Playwright gate green | V2 |
| **I1** | `agentos` CLI (Rust): init/start/send/eval, local runner orchestration via Docker, boxed `start` summary per design | `cli/` | C1, D1 | `agentos send "..."` round-trips a synthetic event through a local runner container and streams the reply, zero Slack involved; `cargo test` + a scripted E2E pass | V0+V1 |

### Wave 3 — The kernel + the wedge

| ID | Task | Owns | Deps | Done when | Verify |
|---|---|---|---|---|---|
| **F1** | **Worker — the prod-hard kernel. Single owner, never split, adversarial review.** Routing rule, finish-race CAS, steer/interrupt, no-retry-after-side-effects, resume-rehydrate | `apps/worker` | D1, E1, G1 | Each rule has an integration test that provokes the race (concurrent send during finish, kill mid-run, side-effect-flagged failure escalates); ordering preserved under concurrent sends | V0+V1+V3 |
| **SK** | **Walking-skeleton gate (verification job, not a build job):** Slack message → dispatcher → worker → claimed sandbox → runner → threaded reply, trace in Langfuse, steering live | — | F1 + resized k8scratch + real Slack workspace | The mvp-build-plan Phase-1 exit criterion demonstrated on k8scratch, recorded run log committed to `docs/` | V3 |
| **J1** | GitHub App, branch→bot identities, promote-on-merge | `apps/api` (gh module) | B1, B2 | Push to dev deploys `@agentos-dev`; merge promotes `@agentos`; identities routed by deployment table | V1 + live GitHub |
| **K1** | Eval runner Jobs + PR checks + eval matrix endpoint | `apps/worker` (eval module) | J1, D1 | PR → eval Jobs fan out → GitHub check reports; matrix endpoint returns the grid across N pinned versions; UI matrix tab renders it | V1+V3 |
| **OB1** | Metrics + Logs observability, Langfuse-backed (design delta, §1): Metrics tab charts (runs, latency, tokens, cost, error rate per agent/env with range selector) served by API endpoints over the Langfuse Metrics API; per-run "runner logs" affordance in trace detail, proxying the K8s pod-logs API for the serving sandbox; `/metrics` (Prometheus format) instrumentation on api/dispatcher/worker for operator scraping — no Prometheus/Loki adoption in-chart | `apps/api` (obs module) + `apps/ui` (metrics/logs tabs) | B1, H1b | Metrics tab renders real series for a seeded workload, verified against Langfuse's own aggregates; trace detail fetches live runner logs from a real sandbox pod; `curl /metrics` on each service returns well-formed Prometheus exposition | V1+V2+V3 |

### Wave 4 — Prod-hard + polish

| ID | Task | Owns | Deps | Done when | Verify |
|---|---|---|---|---|---|
| **A2** | Security rails as chart defaults (the four PT-3-proven rails) | `charts/agentos` (policies) | A1 | The PT-3 probe suite re-runs green against the chart's defaults: egress+metadata blocked with before/after control, secret isolation, non-root/RO-rootfs, gVisor class applied | V3 |
| **L1** | Budgets + kill switch end-to-end (runner enforce → API → UI Cost view) | seam across api/ui/runner (lead-coordinated) | F1, H1b | Test hits per-run ceiling and daily cap, run halts gracefully; kill switch stops a live agent <5s | V1+V3 |
| **N1** | Soak/chaos suite: concurrent threads + mid-thread batch job + sandbox-kill-mid-run + resume-rehydrate | `tests/soak` | F1, G1, A2 | Definition-of-done soak passes 3 consecutive runs on k8scratch: no cross-talk, no duplicate side effects | V3 |
| **O1** | Quickstart docs + demo agent | `docs/` | K1, L1, N1 | Cold reader reaches agent-live-in-Slack from README alone, <15 min | manual |

**Explicitly deferred (design-visible but stubbed):** the interview onboarding wizard (MVP uses the classic create-agent modal), the Memory tab (and automatic memory generation), the real Interview-Me compiler, the Loki live-tail Logs view + kube-prometheus-stack adoption (fleet/leave-behind phase; OB1's per-run log proxy covers MVP debugging), fleet dashboard + drift timeline (state 6), model-gateway toggle. Each gets an empty-state in the UI per the design's own empty-state patterns.

## 5. Orchestration model: how the jobs run

**Mechanism (revised 2026-07-05, Brian's call): all local, no Linear tickets, no remote, no PRs.** This is a prototype — the repo stays local until Brian decides to create a remote. Each task runs as an **in-session sub-agent** spawned by the orchestrator with the standard prompt structure (Goal, Context, Constraints, Done-when — the task row verbatim). Each sub-agent works on a `task/<id>-<desc>` branch; the orchestrator then **verifies the done-when itself** (runs the suites, drives the UI via Playwright MCP, hits k8scratch) and merges `--no-ff` to main. Serialized spine work may land directly on main when branching adds no value; anything parallel always branches.

**The orchestrator loop:** dispatch a wave of sub-agents (disjoint directory ownership) → as each reports, verify its done-when against real output → merge `--no-ff` in dependency order → run the wave's integration checks (compose stack up, cross-package tests, Playwright suite) → dispatch the next wave. Brian reviews asynchronously by reading merged history and the orchestrator's reports; nothing blocks on him. Special handling:

1. **F1 is sacred:** one sub-agent, never split, escalated review before merge (spec-vs-impl + side-effects-detective + a Codex pass on the diff) per the standing adversarial-review guidance for the kernel.
2. **SK and N1 are verification tasks, not build tasks:** their deliverable is a committed run log proving the gate, echoing the PT-1..PT-4 evidence discipline.

**Concurrency and cadence.** Cap at 3-4 concurrent sub-agents — the binding constraints are the weekly token budget and the orchestrator's ability to genuinely verify each result before merging (rubber-stamp merges defeat the whole harness). Wave 1 is the widest point (5 candidates); if capped at 4, hold H1a a beat (it's off the spine). Long-running or token-heavy tasks may alternatively dispatch as local `claude --bg` jobs that commit to their task branch without pushing; the orchestrator still owns verification and the merge.

**Contract-change protocol:** any job needing an `aci-protocol`/`plugin-format` change stops and escalates to the orchestrator (this is in the repo CLAUDE.md); the orchestrator lands the contract change as its own reviewed PR before dependent lanes proceed. The schema-compat CI test enforces this structurally.

## 6. What "the spine" means for sequencing

Brian's stated minimum: the core spine plus a decent chunk of UI + backend. That is exactly **Waves 0-2 + F1 + SK**: after those, a Slack message is answered by a versioned plugin in a claimed sandbox with traces, steering works, the UI authors and deploys plugins and shows runs, and the CLI emulates Slack locally. Waves 3-4 (git-flow wedge, budgets, soak, security defaults) follow the same machinery once the spine demo lands.

Rough shape (calendar depends on review cadence): Wave 0 ≈ 2 jobs sequential, Wave 1 ≈ 5 jobs parallel over 2-3 dispatch cycles, Wave 2 ≈ 4 jobs, then F1 alone, then SK. On a 3-4-jobs-per-cycle cadence that's roughly 5-6 dispatch cycles to the spine demo.

## 7. Needed from Brian (blocking items, in the order they bite)

1. ~~GitHub remote~~ — **decided 2026-07-05: none for now.** All work stays local; Brian creates a remote later if wanted. CI workflows are authored but dormant until then. J1 (git-flow) will need the remote when its wave arrives.
2. ~~Scope calls on the three design deltas~~ — **decided 2026-07-05** (§1): interview wizard deferred, memory tab deferred, Metrics/Logs in MVP Langfuse-backed with a per-run K8s log proxy (task OB1); Prometheus/Loki adoption deferred to the fleet phase.
3. **k8scratch resize** to ≥8 vCPU / 16-20 GB (blocks the SK gate and N1; G1/A2 fit in the current 4 GB): it's an LXC, so this is a Proxmox config change + reboot.
4. **Test Slack workspace + two apps** (blocks SK, J1): one throwaway workspace, two Socket-Mode apps (`@agentos`, `@agentos-dev`), tokens into the repo's gitignored `.env` convention.
5. ~~Linear placement~~ — **decided 2026-07-05: no tickets.** Tracking lives in this doc + the orchestrator's reports; task IDs (R0, C1, ...) are the tracking keys.

## 8. Bottom line

Every infrastructure bet under this plan is already proven live; the design file enumerates exactly what the UI must become; the task DAG has clean file-ownership seams. The orchestration problem reduces to: freeze the contracts (C1), give every job the means to verify itself (R0's harness: compose stack + Playwright suite/MCP + k8scratch + repo CLAUDE.md), then dispatch waves of sub-agents along the DAG with the orchestrator verifying every done-when and running integration gates between waves. The two places judgment must concentrate — the F1 kernel and contract changes — get single ownership and adversarial review by construction.
