# AgentOS MVP: Architecture and Build Order

> **As-built status: built.** This is the plan the MVP was built from; every
> phase (0 through 4) landed, with the single exception of the N1 soak suite
> (scaffold only). For the as-built system rather than the plan, read the root
> [`../ARCHITECTURE.md`](../ARCHITECTURE.md); for the forward work read
> [`roadmap.md`](roadmap.md). This file is kept as the design rationale and the
> record of what the MVP set out to be.

The implementation-ready synthesis for the Curie AgentOS v0.1 MVP. This doc turns the reference design (`detailed-architecture.md`), the on-prem build-vs-adopt research (`on-prem-architecture.md`), and the prototype de-risking runs (`../agent-os-pivot/analysis/agent-os-prototype-derisking-review.md` + its `test-plans/`) into one thing: **what to build, in what order, with the risky infra questions already answered by live prototypes.**

Author: 2026-07-04, after the prototype de-risking passes. Read `detailed-architecture.md` for the full component contract and `../agent-os-pivot/analysis/agent-os-prototype-derisking-review.md` for the evidence behind every "proven" below. This doc is the build spine; those are the reference and the receipts.

## 1. The one-paragraph frame

AgentOS is an open-source, self-hostable developer platform for Slack-based agents: connect Slack, author a Claude-Code-format plugin (skills + tools + MCP) in the browser or a repo, deploy it as a bot identity, and get traces + evals + budgets + git-flow for free. The MVP proves one loop end to end — **a Slack message is answered by a versioned plugin running in an isolated sandbox, with the run traced and steerable** — plus the git-flow and eval-as-CI story that is the actual distribution wedge. Every load-bearing infrastructure choice under that loop has now been validated on a real cluster (see §3); the remaining work is disciplined wiring, not discovery.

## 2. What the prototypes settled (the foundation is proven)

This is why the build can start with confidence. Each row was run live 2026-07-04, not reasoned about (evidence + method in the de-risking review and `test-plans/`).

| Question | Verdict | Evidence |
|---|---|---|
| Does Agent Sandbox give a routable control endpoint into the runner? | **YES** | `Sandbox` + `spec.service:true` → `.status.serviceFQDN` + headless Service; probe pod reached it |
| Does hibernation keep the live process? | **NO — cold restart** | `operatingMode:Suspended` deletes the pod; resume = new pod/UID. Design must rehydrate from history |
| Does warm-pool allocation work? | **YES, ~0.2s** | `SandboxWarmPool` replicas:2 pre-warmed; a `SandboxClaim` bound to a pre-warmed sandbox in **0.199s** (claim Ready timestamp *earlier* than claim creation = inherited a running sandbox); pool auto-replenished, holding readyReplicas:2 |
| Does the runner work *inside* a sandbox (msg → answer → span + cache)? | **YES** | runner in a claimed Sandbox answered via `serviceFQDN`; **cross-request cache reuse proven from inside the sandbox** — call-2 `cache_read_input_tokens=16045` == exactly the 16045 created on call 1; both turns exported nested `agent.run`→`generation` traces to Langfuse (`service.name=agentos-runner-sandbox`) |
| Can the claude-agent-sdk be driven as a long-lived server with steering + interrupt? | **YES** | mid-run message redirected a tool-using agent; `interrupt()` aborted a run |
| Prompt-cache warmth within a session? | **YES within a claim** (not across suspend) | turn-2 `cache_read_input_tokens` = 40,884 |
| Does Langfuse reconstruct the tool-call tree via its public API? | **YES** | 3-level tree via `parentObservationId`; model span → GENERATION |
| Single-node footprint? | **~2.3 GB**, not 16-20 GB | full stack measured; ClickHouse idles ~142 MiB |
| Does the security boundary hold? | **YES** | egress + metadata endpoint blocked (with control), per-agent secret isolation, non-root RO-rootfs, gVisor |
| Can we run headless on an OAuth token (dev)? | **YES** | SDK authed with `CLAUDE_CODE_OAUTH_TOKEN`, no API key |

**The one design constraint that fell out of the evidence:** hibernation cold-restarts the runner. So the MVP is **stateless-first** — session state (conversation history, memory pointer) is externalized, and a resumed thread rehydrates from it. Prompt-cache warmth is an optimization *within* a continuous claim, never assumed across a suspend. This shapes both the worker (F1) and the cost model.

Two on-prem-substrate gotchas the prototypes surfaced, to bake into the chart from day one: (a) a customer CNI must *enforce* NetworkPolicy or the egress lockdown is a silent false-pass — ship a preflight probe; (b) current ClickHouse requires AVX and crashes on older CPUs — pin a SSE4.2-compatible ClickHouse and preflight the CPU.

## 3. MVP architecture (the proven stack)

Nothing here is new relative to `detailed-architecture.md`; this is the subset the MVP builds, annotated with what's proven.

```
Slack ──Socket Mode──> Dispatcher (Bolt) ──enqueue──> Valkey queue
                                                          │
                                                     Worker (routing lock, finish-race,
                                                     steer/interrupt, no-retry-after-side-effects)
                                                          │ claim
                                                          ▼
                                       Agent Sandbox (claimed, spec.service:true)
                                       └─ Runner (claude-agent-sdk streaming server)
                                            ├─ model egress → Anthropic API   [PROVEN]
                                            ├─ OTLP/HTTP spans → OTel Collector → Langfuse   [PROVEN]
                                            └─ plugin bundle @ pinned version (from S3/MinIO)
API server (agents/versions/deployments, git-flow) ── Postgres
UI (skill.md editor, Runs view via Langfuse API)   [Runs view PROVEN buildable on the public API]
CLI (agentos: local emulation, evals)
```

**The ACI (Agent Container Interface)** is the frozen seam: the platform never knows which harness is inside; the harness never knows the platform. For the MVP it is the claude-agent-sdk adapter (streaming-input mode) behind the sandbox's `serviceFQDN`, speaking the session protocol (inject / steer / interrupt / stream NDJSON / budget / side-effect-flag). Freeze `packages/aci-protocol` first — every lane compiles against it, and it is also the versioned contract that de-risks the on-prem upgrade problem.

**Adopt, don't build** (all license-verified in `on-prem-architecture.md`): Langfuse (traces + evals, MIT core), Kubernetes Agent Sandbox (interactive runner substrate), Slack Bolt (Socket Mode), BullMQ/Valkey (queue), Postgres (app state), OTel Collector. Build only: UI, API server, dispatcher, worker+runner glue, CLI, and the Helm umbrella.

## 4. MVP scope: in vs out

**In (v0.1 — the six done-when behaviors from `detailed-architecture.md`):**
1. Connect Slack, write a skill.md in the browser, Deploy, agent answers in-thread within seconds, traces in the Runs view.
2. A follow-up message mid-run steers the in-flight agent; `@agentos stop` interrupts.
3. `agentos start/send/eval` run the plugin locally with no live workspace.
4. Push to `dev` deploys `@agentos-dev`; a PR runs the eval suite as a GitHub check; merge promotes `@agentos`.
5. Budgets enforce (per-run token ceiling, daily USD cap, kill switch); a failed side-effectful run escalates instead of retrying.
6. `helm install` on one 8-10 vCPU / ~20 GB node brings up the whole stack with security rails on by default; the soak test passes with no cross-talk.

**Out (deferred, explicitly):** the Interview-Me workflow compiler (v1.1), automatic memory generation (v1.1), the pi/opencode air-gapped adapter (until a real air-gapped prospect), the fleet dashboard / leave-behind convergence (later), and any generic declarative-DAG engine (never — commodity, no edge).

## 5. Build order

The sequencing rule: **freeze the contract, prove the walking skeleton, then parallelize.** The non-splittable spine is `C1 → aci-protocol → runner → sandbox substrate → worker → soak`. Everything else fans out around it.

### Phase 0 — Contract + scaffold (do first, blocks everything)
- **C1**: monorepo scaffold, CI, and the two frozen packages — `packages/aci-protocol` (typed session-protocol schemas + NDJSON event types) and `packages/plugin-format` (the Claude Code plugin shape verbatim: `plugin.json` + `skills/**/SKILL.md` + `.mcp.json` + `scripts/`). **This is also mitigation #2 for the top operational risk** (the versioned agent↔control-plane contract) — build it as a real versioned interface with a compat test, not an afterthought.
- Exit: both packages typecheck against the section-0 schemas; CI runs lint+test on PR.

### Phase 1 — Walking skeleton (the risky spine; mostly sequential)
Build the one loop that touches every proven piece, hardcoded to a single agent:
- **A1** Helm umbrella v0 (Langfuse + Postgres + Valkey + OTel Collector, dev profile, BYO toggles). *Bake in the two preflights now: CPU-AVX check + a pinned SSE4.2 ClickHouse, and a NetworkPolicy-enforcement probe.*
- **D1** runner image + SDK adapter (the streaming session server — the PT-2 prototype is its seed; add AGENTOS_BUDGET enforcement + `side_effect_flag`).
- **G1** Agent Sandbox substrate (warm pool, `thread_ts → sandbox_id` affinity, `spec.service:true` for the endpoint). *Design for cold-restart resume: rehydrate session from history; do not assume cross-suspend cache warmth.*
- **E1** dispatcher (Bolt Socket Mode, <3s ack + placeholder, enqueue with idempotency key).
- **F1** worker — **the prod-hard kernel, single owner, adversarial review.** Routing lock, finish-race CAS, steer/interrupt, no-retry-after-side-effects, and the resume-rehydrate path.
- Exit: a Slack message is answered by a plugin running in a claimed sandbox, with the trace visible in Langfuse and steering working. (Every dependency of this is already prototype-proven.)

### Phase 2 — Platform core (parallelizes after C1/B1)
- **B1** API server + Postgres schema (agents/versions/deployments CRUD, auth), **B2** plugin bundle pipeline (validate → immutable versioned bundle in S3/MinIO), **H1** UI v0 (skill.md editor + deploy + Runs view — the Runs view is proven buildable on the Langfuse public API).
- Exit: author a plugin in the browser, deploy it, watch its run.

### Phase 3 — Git-flow + evals (the distribution wedge; parallel lane)
- **J1** GitHub App + branch→bot identities + promote, **K1** eval runner Jobs + PR checks + eval matrix, **I1** `agentos` CLI (init/start/send/eval with local Slack emulation).
- Exit: PR runs evals as a GitHub check; merge promotes the bot.

### Phase 4 — Prod-hard (before first client)
- **A2** security rails as chart defaults (NetworkPolicy egress lockdown, per-plugin secret scoping, non-root/RO-rootfs, gVisor RuntimeClass — **all four proven in PT-3**), **L1** budgets + kill switch, **N1** soak/chaos suite (concurrent threads + mid-thread batch job + sandbox-kill-mid-run + the resume-rehydrate path), **O1** quickstart docs.
- Exit: the definition-of-done soak scenario passes 3× with no cross-talk and no duplicate side effects.

**Parallelism:** after C1, up to five lanes start at once (A1, B1, E1, D1, and the eval/CLI prep). F1 is the one lane that must wait for D1+E1+G1 and must not be split. UI (H1) waits on B1. Git-flow (J1/K1) waits on B1/B2. The production auth model (API-key/Bedrock/Vertex per-tenant, not the dev OAuth path) lands in Phase 4 alongside the security rails.

## 6. Risks to manage during the build (not prototype-settleable)

From the de-risking review §8, in the order they'll bite:
1. **The concurrency kernel (F1).** Correctness logic; retire it with the soak test, not a spike. Now compounded by cold-restart resume — the finish-race and the rehydrate path interact, and that's where duplicate side effects hide. Single owner, adversarial review.
2. **The versioning contract.** Freeze `aci-protocol` + `plugin-format` first (Phase 0) with a compat CI test; it's process discipline the team lacks, so make it structural.
3. **Cache economics under hibernation.** Budget/pricing model assumes cache-cold on resume; hibernation TTL is a cost lever. Never put a content-based model router in front of the harness (breaks the prefix cache).
4. **agent-sandbox is pre-1.0.** Keep the ACI abstraction + a plain-K8s-Job fallback for interactive threads; warm-pool is the one extension surface to keep watching.
5. **On-prem substrate variance.** The CNI-enforcement probe and the ClickHouse CPU preflight are Phase-1 chart items, not afterthoughts.
6. **Production auth.** OAuth proved the mechanism; per-tenant API-key/Bedrock/Vertex is real Phase-4 work.

## 7. Decisions for the implementation-ordering conversation

Bring these to the planning talk (they set sequencing, not feasibility):
1. **First-client funding shape.** The plan says the first AWS-hosted client engagement funds Phases 1-4 with the platform/plugin layers kept separate. Does that hold, and does it change which lanes get staffed first?
2. **TS end-to-end vs a Python runner.** The plan leans TS (Bolt-js + BullMQ + one runtime); the runner adapter can be TS or Python (the SDK ships both). Decide before D1.
3. **Warm-pool now or Job-fallback first.** Warm pool is proven-ish (pending the sub-agent's PT-D result) but pre-1.0; is the MVP's interactive path warm-pool from the start, or Job-fallback first with warm-pool as a fast-follow?
4. **How much of the leave-behind observability ships in v0.1** vs waits for the fleet/leave-behind phase — this is the revenue hook, so it may deserve to be pulled earlier than "later."
5. **UI scope for v0.1.** The Claude design (arriving tomorrow) sets the UI surface; decide how much of states 4-6 (eval matrix, fleet) is MVP vs fast-follow once we see it.

## 8. Bottom line

The foundation is proven end to end on a real cluster; the one architectural constraint (hibernation cold-restart → stateless-first) is understood and designed for. The MVP is a disciplined-wiring problem, not a research problem. Start with the frozen contract, prove the walking skeleton (every piece of which is already validated), then fan out — and spend the reserved engineering judgment on the concurrency kernel and the versioned contract, which are the only two places the build can still go wrong in a way a prototype couldn't have caught.
