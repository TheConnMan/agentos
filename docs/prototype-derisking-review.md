# Curie AgentOS: Prototype De-risking Review

> **As-built status: historical (the receipts).** This is the pre-build
> evidence: the live prototype runs that settled the risky infrastructure bets
> before any of the MVP was built. Every verdict here held up; the system that
> got built on top of it is described in the root
> [`../ARCHITECTURE.md`](../ARCHITECTURE.md). Kept unchanged as the record of why
> the build could start with confidence.

Author: prototype-derisking pass, 2026-07-04. Companion to `.projects/agent-os-pivot/agent-os-product-direction.md` (strategy), `agent-architecture-decision-menu.md` (the 10-axis menu), and `.projects/supabase-for-agents/detailed-architecture.md` (the build plan for "AgentOS", the v0.1 platform). This document exists to answer one question: **which architecture assumptions in the AgentOS build plan are most likely to be wrong, and which of those can a cheap mini-prototype settle before the full build starts.**

Scope note. The strategy doc argues *what* to build and where the moat is (verification oracle + eval reproducibility). This review deliberately does not re-litigate the strategy. It targets the **implementation architecture** in `detailed-architecture.md`, because that is where the Phase-0 spikes live and where the money gets spent. Where a strategy risk is unavoidable I flag it and move on.

> **What actually got run (2026-07-04, two passes).** Pass 1: inspected the real agent-sandbox v0.5.0 CRD schema, then ran **PT-1 live on the `k8scratch` throwaway k3s cluster**. Pass 2 (after Brian authorized the OAuth token, gVisor install, and a capped ClickHouse run): **PT-2 core, PT-4 full, and a gVisor/Kata install on k8scratch.** Headline results, all from real output:
> - **PT-1 (R1/R2):** routable endpoint HOLDS (`Sandbox` + `spec.service:true` → `.status.serviceFQDN`, probe pod reached it); **hibernation is a COLD RESTART** (`operatingMode:Suspended` deletes the pod; resume = new pod/UID) → no cache warmth survives suspend.
> - **PT-2 core (R1/R2/R7), driven by Brian's OAuth token headless:** auth works with no API key; **steering at tool-loop boundaries is real** (agent abandoned a 5-step task to obey a mid-run redirect); **interrupt aborts a live run**; **cache_read within a session confirmed** (turn 2 read 40,884 cached tokens). So cache warmth exists *within a continuous claim* but not across a suspend.
> - **PT-4 full (R3/R6):** Langfuse OTLP-HTTP ingest works, model span → GENERATION, and the **public API reconstructs the 3-level tool-call tree via `parentObservationId`** (S1 = GO, no ClickHouse-SQL fallback needed). **Footprint = 2.28 GiB** for the whole backbone at light load (vs the doc's ~16-20 GB) — ClickHouse idles at ~142 MiB; the footprint fear is the Helm preset, not ClickHouse. NEW caveat: current ClickHouse needs **AVX**; it SIGILL-crashed on this SSE4.2-only host until pinned to `:24.8` — an on-prem CPU-baseline preflight item.
> - **PT-3 (R4):** gVisor/Kata install attempted on k8scratch — see R4 for the verdict (the box is a 4 GB LXC with no `/dev/kvm`).
>
> **The one deliverable Brian named that is absent: the "Claude design file" — no such file had arrived under the project tree as of 2026-07-04; treated as an assumption, not a blocker.**

---

## 1. How I read the plan (the load-bearing claims)

The AgentOS build plan (`detailed-architecture.md`) is unusually concrete for a pre-build doc, which makes it reviewable. Its architecture rests on a small number of load-bearing bets. Stated plainly, so we can attack them:

- **B1 — Kubernetes Agent Sandbox is the interactive runtime primitive.** The plan adopts `kubernetes-sigs/agent-sandbox` and its `SandboxWarmPool` as the substrate for interactive Slack threads, and assumes it provides: stable per-thread identity, a **routable control endpoint into the running process**, hibernation/resume, persistent scratch, and warm-pool allocation. The ACI (section 0) is a *session-scoped bidirectional* protocol layered on that endpoint. (`detailed-architecture.md:20`, `:151-172`)
- **B2 — The claude-agent-sdk streaming-input mode makes Claude-Code-style steering "not an emulation."** A follow-up Slack message is injected into the live run at the next loop boundary; interrupt is native. (`:49`, `:140-149`)
- **B3 — Langfuse (MIT core) is the single adopted backbone for traces AND evals**, and its public API is strong enough to reconstruct a tool-call tree for the Runs view. (`on-prem-architecture.md:9`, `:74`; spike S1 at `:356`)
- **B4 — Prompt-cache warmth survives the sandbox-affinity path**, i.e. routing the same thread to the same live sandbox yields `cache_read_input_tokens > 0`. This is a cost-model assumption, not just a perf nicety. (`:172`, `:216`)
- **B5 — Security rails (NetworkPolicy egress lockdown, per-plugin secret scoping, non-root/read-only-rootfs, interrupt kill path) are chart defaults that actually hold** against a prompt-injectable Slack input channel while the runner holds customer credentials. (`:226`)
- **B6 — The whole stack fits one 8-10 vCPU / ~20 GB node** including the Langfuse backbone (ClickHouse is the anchor). (`:14`, `on-prem-architecture.md:114`)
- **B7 — The concurrency kernel** (per-thread routing lock, finish-race CAS, steer-vs-interrupt, no-retry-after-side-effects) is correct as specified and is the "prod-hard kernel." (`:138`, `:140-149`)
- **B8 — The plugin bundle format is the Claude Code plugin shape verbatim**, and that format runs natively under the claude-agent-sdk adapter with zero translation. (`:265`, `:52`)

The plan already names three Phase-0 spikes (`:232`): S1 Langfuse tool-tree, S2 ACI round-trip, and cache-read-through-affinity. That instinct is correct. This review sharpens *what each spike must prove to count as a GO*, adds spikes the plan under-weights (security-boundary and the concurrency kernel), and reranks by blast radius.

I verified several external facts directly (2026-07-04) so the ranking rests on current reality, not the plan's 07-02 snapshot:

- **agent-sandbox is at v0.5.0 (released 2026-06-24), still carries no alpha/beta/GA maturity label.** The docs name four CRDs (`Sandbox`, `SandboxTemplate`, `SandboxClaim`, `SandboxWarmPool`) with gVisor + Kata runtime classes, TTL cleanup, hibernation, Python + Go client SDKs, install via `kubectl apply -f .../releases/download/${VERSION}/manifest.yaml`. Source: github.com/kubernetes-sigs/agent-sandbox, agent-sandbox.sigs.k8s.io/docs. **This is the single least-mature load-bearing dependency in the stack.**
- **I pulled and inspected the actual v0.5.0 `manifest.yaml` CRD schema (396 KB, 2026-07-04) — three findings that materially update R1 below, ahead of any cluster run:**
  1. **The routable-endpoint assumption (B1) is CONFIRMED at the schema level.** The `Sandbox` CRD (`sandboxes.agents.x-k8s.io`, served versions `v1beta1` and `v1alpha1`) exposes in `.status`: **`service` and `serviceFQDN`** (plus `podIPs`, `nodeName`, `conditions`, `selector`). So a claimed sandbox *does* advertise a stable DNS/Service the worker can dial. This is the single biggest R1 unknown, and it lands in the plan's favor.
  2. **Hibernation is coarser than the plan implies, and this HURTS the cache-warmth bet (R2).** The only lifecycle control in `spec` is **`operatingMode` (enum: `Running` | `Suspended`)** plus **`shutdownPolicy` (enum: `Delete` | `Retain`)** and **`shutdownTime`**. There is no "keep the process live across resume" primitive — a `Suspended` sandbox is a paused/scaled-down pod, so **resume is almost certainly a cold process restart with `Retain` only preserving the scratch volume, not the in-RAM agent loop.** That is exactly the R1-PARTIAL / R2-lost scenario. PT-1's hibernate/resume step now has a concrete hypothesis to confirm: `operatingMode: Suspended → Running` restarts the process.
  3. **Only the `Sandbox` CRD ships in the core `manifest.yaml`.** `SandboxTemplate`, `SandboxClaim`, and `SandboxWarmPool` are NOT in it — they are a separate "extensions" install. The plan's warm-pool allocation story (`detailed-architecture.md:151-172`) depends on an extension component that must be installed and verified separately, and whose maturity is even less certain than the core controller. PT-1 must install and exercise the extensions explicitly, not assume they come with the controller.
- **Langfuse ingests OTLP natively at `/api/public/otel/v1/traces` over HTTP/JSON or HTTP/protobuf; gRPC is NOT supported yet.** Any span with a `model` attribute becomes a `generation` observation; it "requires that a root span is sent" or the trace is malformed. Source: langfuse.com/integrations/native/opentelemetry. This directly bears on B3 and on the ACI's `OTEL_EXPORTER_OTLP_*` contract (`:46`) — **if the collector or SDK is configured for gRPC OTLP, traces silently vanish.**
- **claude-agent-sdk streaming-input mode is real and documented**: input is an async generator, messages can be queued mid-session, `interrupt` is a control method, images attach inline. Source: code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode. B2's *mechanism* is sound; the open question is the server-side hosting shape, not the SDK capability.
- **Langfuse chart v1.5.37 / app v3.201.1** pulls ClickHouse/Valkey/MinIO/Postgres as Bitnami OCI subcharts, ClickHouse defaulting to a 3-replica 2xlarge cluster — the B6 footprint trap the on-prem doc already flags is real and current.

---

## 2. The ranking model

Each risk is scored on four axes, 1-5:

- **Severity** — if this assumption is wrong, how much of the build is invalidated or forced into a redesign.
- **Uncertainty** — how unproven the assumption is *today* (a documented, GA capability scores low; a pre-1.0 dependency doing something its docs only imply scores high).
- **Prototype feasibility** — how cheaply and safely a mini-prototype can settle it (5 = a few hours on a scratch cluster; 1 = needs real customer env / weeks).
- **Blast radius** — how many downstream tasks in the section-11 DAG depend on this being true.

A risk is a **prototype candidate** when Severity and Uncertainty are both high AND feasibility is high. High severity + low feasibility goes to "de-risk by design/contract, not by prototype" (section 5). Low uncertainty goes to "don't prototype" (section 6).

---

## 3. Risk register (ranked)

> **PT-1 was RUN on the `k8scratch` throwaway cluster (2026-07-04).** R1's routing sub-claim is now CONFIRMED live; R1/R2's hibernation sub-claim is CONFIRMED as a cold restart. See the updated verdicts inline below and the full run log in `../test-plans/PT-1-agent-sandbox-lifecycle.md`.

### R1 — Agent Sandbox does not deliver the interactive control endpoint the ACI assumes  ★ top prototype — PARTLY RESOLVED by PT-1
**Severity 5 · Uncertainty 5→2 (routing proven) · Feasibility 4 · Blast radius 5**

**PT-1 live result (2026-07-04):** on real k3s, a `Sandbox` with `spec.service: true` populates `.status.serviceFQDN` and auto-creates a headless Service that a probe pod reached (`exit=0`). The worker's dial target exists and works. Suspend (`operatingMode: Suspended`) **deletes** the pod; resume creates a **new pod (new UID, new start time)** — the live process does not survive, though `serviceFQDN` re-binds durably. So R1 decomposes to: sub-claim 1 (routable endpoint) **PROVEN**; sub-claim 2 (our in-sandbox control server) still to build+prove in PT-2; sub-claim 3 (live process across resume) **DISPROVEN — resume is a cold rehydrate**, which is designable-around but must be designed for. Warm-pool (extensions CRDs) remains unproven (not in the core manifest). Original analysis retained below for the reasoning trail.

The ACI (`:38-49`) needs a **bidirectional control channel into a running process** — inject a follow-up message mid-run, stream NDJSON back, deliver an interrupt — over "HTTP/2 or WebSocket on the sandbox control endpoint, while claimed." The plan asserts Agent Sandbox provides "the routable endpoint to the harness" (`:20`).

Here is the gap. Agent Sandbox's documented model is "a single stateful pod with stable hostname and optional persistent storage" plus lifecycle (warm pool, hibernate/resume, TTL). That gives you a **stable network identity and a pod you can reach** — it is *not* a documented application-level control protocol that pushes a message into an already-running agent loop. The plan even concedes this in section 8 open-decision #2: "confirm current CRDs, control-channel route, hibernation/resume hooks, and scratch persistence before implementation." That is the whole ballgame for interactive threads, listed as an open decision.

What actually has to be true for the plan to work:
1. A claimed sandbox exposes a **stable, routable endpoint** (Service/FQDN in status) the worker can dial while the pod is Running.
2. **Something inside the sandbox** listens on that endpoint and can accept a new input while the agent loop is mid-turn — this is *our* long-lived server wrapping the claude-agent-sdk `query()` async generator, not anything Agent Sandbox ships. The SDK supports the push (B2); the plan must build the HTTP/WS server that owns the generator and multiplexes inject/interrupt/stream onto it.
3. Hibernate/resume must preserve the in-process session (or the design must degrade gracefully to a rehydrate-from-history cold start — and the plan's cache-warmth claim B4 quietly assumes it does NOT cold-start).

The most likely failure mode: Agent Sandbox gives you (1) but (2) is entirely our code and (3) does not preserve a live Python/Node process across hibernation. **The v0.5.0 CRD schema (inspected 2026-07-04) sharpens this from a guess to a near-certainty:** the lifecycle control is `operatingMode: Running|Suspended` with `shutdownPolicy: Retain|Delete` — a `Suspended` pod is paused/scaled-down and `Retain` preserves only the volume, so resume is a cold process restart, not a live socket + in-RAM agent-loop resume. If resume is a process restart, then "steering into the live run" only works *within* a single un-suspended claim, and every resume is a cold rehydrate — which also kills the prompt-cache assumption B4. **This one assumption, if wrong, reshapes the entire interactive path (F1, G1, D1 in the DAG).** Design consequence to carry forward regardless of PT-1's result: the harness must be able to **rehydrate a session from stored history on resume** (statelessness as the floor, warmth as the optimization — which the plan already states at `detailed-architecture.md:34`), and the cost model must NOT assume cache warmth survives a suspend/resume cycle.

Good news from the same schema inspection: sub-claim (1) is confirmed — `.status.service` + `.status.serviceFQDN` exist, so the worker *can* dial a claimed sandbox. The residual R1 risk is concentrated in (2) our in-sandbox control server (covered by PT-2) and (3) the suspend/resume-is-cold-start behavior (covered by PT-1's hibernate step). Note also that `SandboxWarmPool`/`SandboxClaim`/`SandboxTemplate` are a *separate extensions install* (not in the core manifest), so the warm-pool allocation path is an additional pre-1.0 surface PT-1 must install and prove, not something bundled with the controller.

**UPDATE (2026-07-04, PT-D + PT-E run on k8scratch):** both remaining R1 sub-claims are now settled GO. **Warm-pool works, sub-second:** installed the `extensions.yaml`, created a `SandboxWarmPool` (replicas:2) that pre-warmed two sandboxes, and a `SandboxClaim` bound to a *pre-warmed* sandbox in **0.199s** (the tell: the claim's Ready timestamp is *earlier* than its own creation — it inherited a running sandbox, not a cold create), with the pool auto-replenishing to readyReplicas:2 after each claim. **The runner-in-sandbox works, with in-sandbox cache reuse:** a `claude-agent-sdk` runner container ran inside a claimed `Sandbox` (`spec.service:true`, OAuth token via Secret), answered HTTP `POST /run` calls via its `serviceFQDN` with real Anthropic responses, and **carried the prompt cache across requests inside the live session** — call-2 `cache_read_input_tokens=16045`, exactly the 16045 tokens created on call 1. Both turns exported nested `agent.run`→`llm.generation` gen_ai traces to Langfuse over the tailnet (verified independently: two traces, `service.name=agentos-runner-sandbox`, correct parent linkage). So the full interactive chain — warm-pool claim (~0.2s) → routable endpoint → in-sandbox SDK session with cache affinity → traced run — is proven end to end on a real cluster. The cold-restart caveat (sub-claim 3) stands: design for rehydrate-on-resume.

Prototype: **PT-1 (sandbox lifecycle + control-endpoint reachability)** and **PT-2 (ACI round-trip on the sandbox)**. Settle whether a claimed sandbox is reachable, whether our in-sandbox server can accept a mid-run steer over that route, and what hibernate/resume does to a live process.

### R2 — Prompt-cache warmth does not survive the affinity path (cost-model risk)  ★ prototype — SHARPENED by PT-1
**Severity 4 · Uncertainty 4 · Feasibility 3 · Blast radius 3**

**PT-1 finding raises this from "at risk" to "confirmed lost across hibernation":** because suspend deletes the pod and resume cold-starts a new one, there is no live process to hold a warm prompt prefix across a suspend/resume cycle, and the 5-minute cache TTL is irrelevant once the pod is gone. Cache warmth can therefore only ever exist **within a single continuous claim** (consecutive turns before any suspension), which is exactly what PT-2 still needs to measure. The budget/cost feature (`detailed-architecture.md:224`) must model the common case as cache-cold on resume, not cache-warm.

B4 is stated as a smoke-test criterion (`cache_read_input_tokens > 0` on a sandbox-affined exchange, `:14`, `:172`) and the plan is explicit that Claude Code economics *depend* on the 90% prompt-cache discount and its 5-minute TTL (`:216`). The cache is model-scoped and prefix-based with a 5-minute TTL. Two things can silently break it:

1. **Hibernation between turns exceeds the 5-minute TTL** or restarts the process, so the cached prefix is gone and every "warm resume" is actually a full-price cache miss. If R1's resume-is-cold-start turns out true, R2 is automatically lost and the per-thread cost model in section 6b (budgets) is built on a false floor.
2. **A model gateway (LiteLLM) in the path reshapes/reorders the request** so `cache_control` breakpoints no longer land byte-identically (`:213`). The plan already flags this as conditional; the risk is shipping the gateway toggle without a cache-verification gate.

This is a *cost* risk, not a correctness risk, which is why it is severity-4 not 5 — but the plan sells "cost control is a product feature" (`:224`) and a broken cache turns a $X/run agent into a ~$3-10X/run agent invisibly. Worth an explicit measurement, folded into PT-2.

### R3 — Langfuse trace/eval API cannot reconstruct the tool-call tree the Runs view needs  ★ RESOLVED by PT-4
**Severity 4 · Uncertainty 3→0 (proven) · Feasibility 5 · Blast radius 4**

**PT-4 ran the full Langfuse stack (2026-07-04): R3 is a GO.** OTLP-HTTP ingest worked, a `model`-bearing span mapped to `type=GENERATION`, and `GET /api/public/observations?traceId=…` returned populated `parentObservationId` linkage that reconstructed the exact 3-level tree (`agent.run` → `llm.generation` → two `execute_tool` TOOL observations). No ClickHouse-SQL fallback needed. Two bonus findings: **(a)** the single-node footprint is **2.28 GiB**, not the doc's 16-20 GB — ClickHouse idles at ~142 MiB, so R6 is comfortably fine on one node and the Helm 3-replica preset is the only real trap; **(b)** current ClickHouse requires **AVX** and SIGILL-crashed on this SSE4.2-only host until pinned to `:24.8` — a CPU-baseline preflight the on-prem chart should add. Original reasoning retained below.

The whole product surface (H1 Runs view, the eval matrix, the leave-behind) reads from Langfuse. The plan bets the public API's parent-observation linkage reconstructs a 3-level tool-call tree (`on-prem-architecture.md:121` names this as "the one 'is the API strong enough' item worth a spike"; S1 at `:356`). I confirmed Langfuse ingests OTLP natively and maps `gen_ai.*`/`model`-bearing spans to generation observations, and that it *requires a root span* or the trace is malformed. The residual uncertainty is moderate (the data model supports nesting; the question is whether *our* gen_ai span emission produces clean parent links, and whether the OTLP-vs-SDK ingestion path preserves them). Feasibility is very high — this needs no cluster at all, just a local Langfuse (docker compose) + a scripted trace. **Do S1 exactly as planned; add the explicit gRPC-vs-HTTP gotcha to its checklist** (gRPC OTLP is silently unsupported; the ACI's `OTEL_EXPORTER_OTLP_*` must be HTTP/protobuf).

### R4 — The security boundary does not hold against a prompt-injectable input channel  ★ RESOLVED by PT-3
**Severity 5 · Uncertainty 3→1 · Feasibility 4 · Blast radius 3**

**PT-3 ran fully on k8scratch (2026-07-04): the security boundary holds.** (1) **Egress lockdown works, with a control** — baseline egress got HTTP 200; after default-deny + allow-DNS-only, `example.com` and the `169.254.169.254` metadata endpoint both blocked (curl exit 7) while DNS still resolved. The before→after flip proves k3s's NetworkPolicy controller genuinely enforces, and the classic metadata-endpoint escape is closed. (2) **Cross-agent secret isolation** — an RBAC Role scoped to one secret let SA `agent-a` get its own creds but not agent-b's, and not list. (3) **Non-root/read-only-rootfs** — pod ran uid=1000, write to `/` failed read-only. (4) **gVisor** kernel isolation installed and proven. Kata infeasible on this box (no `/dev/kvm`). Residual (not a k8s issue): a *customer's* CNI must also enforce NetworkPolicy — verify per-install with the same before/after probe, since a non-enforcing CNI is a silent false pass. Original analysis below.

The plan correctly identifies the threat (`:226`): Slack input is prompt-injectable by anyone in the channel, and the runner holds customer credentials. It ships NetworkPolicy egress lockdown, per-plugin secret scoping, non-root/read-only-rootfs, per-agent ServiceAccounts as *chart defaults*. This is the right instinct, but the plan treats it as a done design decision (A2 in the DAG), not something to prove. Two reasons to prototype it early:

1. **NetworkPolicy egress lockdown is famously easy to get wrong** and famously easy to *believe* you got right. A policy that looks restrictive can still permit egress (no default-deny, a permissive CNI, DNS wildcard, a metadata-endpoint hole). The Unit 42 AgentCore DNS/S3 egress escape (Apr 2026, cited at `on-prem-architecture.md:88`) is the exact class of bug: a sandbox that was *supposed* to be egress-locked wasn't. If Curie's leave-behind ships to a security-reviewing enterprise, "we thought the NetworkPolicy blocked it" is an incident.
2. **agent-sandbox at pre-1.0 with gVisor/Kata is a config surface, not a guarantee.** Whether the runtime class is actually enforced (vs silently falling back to runc) is testable and worth testing.

A cheap prototype proves default-deny egress actually blocks an unlisted host from *inside* a running sandbox pod, and that a pod cannot read another agent's secret. This is **PT-3**. High severity (it is a security boundary), moderate uncertainty (NetworkPolicy semantics are known but error-prone), high feasibility (a curl from inside a pod).

### R5 — The concurrency kernel (finish-race CAS, steer vs queue, no-retry-after-side-effects) has a correctness bug  ★ de-risk by focused build + test, not a throwaway prototype
**Severity 5 · Uncertainty 3 · Feasibility 2 · Blast radius 5**

The plan itself names this the "prod-hard kernel" and marks the F1 lane non-splittable (`:376`). The finish-race (`:145`: "a message arriving as the run completes needs atomic check-and-deliver") is a classic compare-and-swap hazard; the "no auto-retry after side effects" rule (`:148`) guards against duplicate side-effectful tool calls under at-least-once delivery. These are real, and they are *logic* bugs, not infrastructure unknowns — you cannot settle them with a throwaway cluster prototype; you settle them with a correctly-designed state machine and an integration/soak test (N1 in the DAG). Severity and blast radius are max, but prototype feasibility is low because the thing you'd build to test it *is* the real thing. **Recommendation: do not spin a throwaway prototype; instead front-load the N1 soak scenario as an executable test harness against the walking skeleton, and treat F1 as the one lane that gets adversarial review.** Listed here so it is not mistaken for a prototype candidate — it is a build-discipline item.

### R6 — Single-node footprint (B6) is under-estimated; ClickHouse OOMs the node  — RESOLVED (over-estimated, not under)
**Severity 3→1 · Uncertainty 2→0 (measured) · Feasibility 5 · Blast radius 2**

**PT-4 measured it: the estimate was too HIGH, not too low.** The whole Langfuse backbone idled at **2.28 GiB** (web 1.28 GiB is the biggest single piece; ClickHouse only 453 MiB, idling at 142 MiB standalone). A single-replica ClickHouse comfortably fits one small node. The only real footprint trap is the Helm `clickhouse.replicaCount: 3` / `2xlarge` default (override to 1). The genuinely new operational risk PT-4 surfaced is not RAM but **CPU instruction baseline**: current ClickHouse needs AVX and hard-crashes (SIGILL) on SSE4.2-only hosts — pin ≤24.8 or add a preflight for constrained on-prem CPUs.

The on-prem doc already flags the ClickHouse 3-replica/2xlarge default trap and estimates ~8-10 vCPU / ~20 GB for the dev profile. This is low-uncertainty (it is a known Helm-values override) and trivially testable, and it rides along for free inside PT-4 (Langfuse helm install). Not worth a dedicated prototype, but the PT-4 plan captures the actual resident memory so the "one node" claim is evidence-backed rather than estimated.

### R7 — claude-agent-sdk cannot be hosted as a long-lived multi-tenant server the way the ACI needs  — LARGELY RESOLVED by PT-2
**Severity 4 · Uncertainty 2→1 · Feasibility 4 · Blast radius 4**

**PT-2 drove the SDK (0.2.110) headless as a streaming session and it behaved as the ACI needs:** a long-lived `ClaudeSDKClient` accepted a mid-run message that steered the agent at the next tool-loop boundary, `interrupt()` aborted a live run, and prompt caching worked across turns. One-process-per-sandbox (the plan's model) sidesteps multi-tenant-in-one-process entirely. Residual: wrapping this in the HTTP/WS server and running it inside a sandbox is still to build, but no SDK capability gap was found.

Distinct from R1. R1 is "can the sandbox route to a process"; R7 is "can our process own an SDK `query()` generator per thread, long-lived, and multiplex many of them." The SDK streaming mode is documented and supports the push/interrupt (B2 confirmed). Residual uncertainty is low-moderate: one process per sandbox (the plan's model) sidesteps the multi-tenant-in-one-process problem entirely, since each sandbox runs exactly one thread's agent. The real question folds into PT-2 (does one long-lived SDK session in a container accept a mid-run message and stream deltas). Kept separate in the register so the reviewer sees it is *covered* by PT-2, not skipped.

### R8 — Plugin bundle format compatibility (B8) is assumed, not verified against the SDK adapter
**Severity 3 · Uncertainty 2 · Feasibility 5 · Blast radius 3**

The distribution wedge is "the Claude Code plugin shape verbatim" (`:265`). The claude-agent-sdk adapter is claimed to "run the plugin format natively" (`:52`). This is largely true (the SDK is the harness behind Claude Code) but the exact bundle layout — `plugin.json` + `skills/**/SKILL.md` + `.mcp.json` + `scripts/` — loading cleanly headless in a container, with MCP servers actually starting, is worth a 30-minute confirmation. Folds into PT-2 as a sub-check (load a real 2-skill + 1-MCP plugin and confirm a tool from the MCP is callable). Not a standalone prototype.

### R9 — Slack Socket Mode reliability on a single-node self-host
**Severity 2 · Uncertainty 2 · Feasibility 3 · Blast radius 2**

Slack recommends HTTP for production; Socket Mode needs supervised reconnect. Real but well-trodden and low-blast-radius (the dispatcher E1 is isolated). **Do not prototype now**; handle in E1 with reconnect supervision. Noted for completeness.

### R10 — Model egress / air-gapped tier (pi/opencode adapter)
**Severity 2 · Uncertainty 3 · Feasibility 2 · Blast radius 1**

The plan explicitly defers this ("only when a real air-gapped prospect exists," `:254`). Correct call. **Do not prototype now.**

---

## 4. Prototype selection (what to actually build)

Ranked by value-per-hour. All target the scratch cluster or a local docker/kind, never a shared Curie environment.

| ID | Prototype | Settles | Sev·Unc | Feasible | Timebox | Env | Run status (2026-07-04) |
|----|-----------|---------|---------|----------|---------|-----|-----|
| **PT-1** | Agent Sandbox lifecycle + control-endpoint reachability | R1 (part), R6-adjacent | 5·5 | High | 0.5 day | k8scratch | **RUN** — GO w/ cold-restart caveat |
| **PT-2** | ACI round-trip: streaming steer + interrupt + gen_ai spans, incl. plugin load + cache-read | R1 (part), R2, R7, R8 | 5·5 / 4·4 | Med-High | 1-1.5 day | local (OAuth) + k8s | **RUN** — auth/steer/interrupt/cache PASS (local); **runner-in-sandbox + span export PROVEN** (PT-E on k8scratch) |
| **PT-D** | Warm-pool allocation (extensions): pre-warm + claim binds to warm sandbox | R1 (warm-pool) | 4·4 | High | 0.5 day | k8scratch | **RUN — PASS** (replicas:2 pre-warmed; claims bound to warm; pool replenished) |
| **PT-E** | Runner inside a Sandbox: msg via serviceFQDN → answer → nested gen_ai spans to Langfuse | R1, R7, R3 | 5·4 | Med | 0.5 day | k8scratch + local Langfuse | **RUN — PASS** (2 turns traced from `agentos-runner-sandbox`) |
| **PT-3** | Security boundary: egress lockdown; cross-agent secret isolation; runtimeClass enforced | R4 | 5·3 | High | 0.5 day | k8scratch | **RUN — all claims PASS** (egress+metadata blocked w/ control, secret isolation, non-root RO-rootfs, gVisor); Kata infeasible |
| **PT-4** | Langfuse: OTLP-HTTP ingest + tool-tree reconstruction via public API; footprint | R3 (=S1), R6 | 4·3 / 3·2 | Very High | 0.5 day | local docker | **RUN** — GO; footprint 2.28 GiB (not 16-20) |

Sequencing: **PT-4 and PT-3 are independent and can run first/in parallel** (no Anthropic key needed for PT-4's ingest test; PT-3 needs only a cluster). **PT-1 gates PT-2** (must have a reachable sandbox before testing the control channel). PT-2 is the expensive one (needs a working Anthropic credential reachable from the cluster, and it burns real tokens) and the highest-value — it is the single experiment that most reduces build risk, because it exercises R1/R2/R7/R8 together in one round-trip.

Mapping to the plan's own spikes: PT-4 **is** S1 (plus the footprint capture and the gRPC gotcha). PT-2 **is** S2 (plus cache-read and plugin-load sub-checks). PT-1 and PT-3 are **additions** the plan folded into "open decision #2" and the A2 lane respectively — this review pulls them forward into Phase 0 because their blast radius (R1) and severity (R4) warrant evidence before the walking skeleton, not during it.

Each prototype has a standalone plan file under `../test-plans/`.

---

## 5. De-risk by design/contract, not by prototype

These are high-severity but a throwaway prototype is the wrong instrument:

- **R5 concurrency kernel** — front-load the N1 soak scenario as the acceptance test for F1; give F1 adversarial review; do not split the lane. (Build discipline, not a spike.)
- **The agent↔control-plane versioning red flag** (strategy doc Layer 2, `agent-os-product-direction.md:80`, and Brian's own top comment in `.comments.json`). This is the biggest operational risk in the *whole pivot* and it is a *contract-discipline* problem (backwards-compatible versioned API, support-window policy, auto-upgrade), not something a cluster prototype settles. De-risk it by writing the versioned ACI/plugin-format contract as `packages/aci-protocol` first (the plan already makes C1 build it first) and adding a contract-compat test to CI. Flagged so it is not lost among the infra prototypes.
- **agent-sandbox pre-1.0 dependency risk** — the mitigation is architectural (the plan's "plain-K8s-Job fallback" and the ACI abstraction), not a prototype. PT-1 should, however, record *how much* of the interactive story depends on warm-pool/hibernation specifically, so the fallback's cost is known.

---

## 6. What NOT to prototype now (and why)

- **R9 Socket Mode reliability** — well-understood; handle with reconnect supervision in E1. A prototype teaches nothing new.
- **R10 air-gapped / pi-opencode adapter** — correctly deferred by the plan until a real air-gapped prospect exists. Prototyping it now is speculative.
- **Git-flow bot identities / eval-as-CI (J1/K1)** — mechanical (two Slack app tokens + a routing column; GitHub check status API). Low uncertainty, no infra unknown. Build it, don't spike it.
- **The declarative-DAG / config-as-definition engine** — the strategy doc explicitly says do NOT build a generic DAG engine (`agent-os-product-direction.md:98`). Nothing to prototype; the Phase-0 investment is the `agent_definitions` config-hash stamp, which is a schema change, not an infra experiment.
- **Interview Me compiler / automatic memory generation** — explicitly v1.1 backlog, off the MVP critical path (`:195`, `:242`). Do not prototype.
- **Budgets/kill-switch mechanics (L1)** — a straightforward runner-side counter + a control message; the *only* budget assumption worth checking is that the token accounting is real, which PT-2 captures for free (it reads `cache_read_input_tokens` etc. off the run).

---

## 7. The one-paragraph verdict for the meeting

**Updated after the 2026-07-04 prototype runs.** The AgentOS plan's biggest technical unknowns have now been largely settled with live evidence, and the news is mostly good. **Agent Sandbox does deliver a routable control endpoint** (`.status.serviceFQDN`, reached by a probe pod) — R1's core routing bet holds. **The claude-agent-sdk supports real mid-run steering and interrupt** and prompt caching within a session — R7/R2-within-claim confirmed. **Langfuse reconstructs the tool-call tree via its public API** and the whole backbone fits in **~2.3 GB, not 16-20 GB** — R3/R6 resolved in the plan's favor. **gVisor isolation runs** on the target substrate. The security boundary (R4) has now also been proven end to end on k8scratch: default-deny egress blocks arbitrary hosts and the cloud metadata endpoint (verified with a before/after control on a genuinely-enforcing NetworkPolicy CNI), per-agent secrets are RBAC-isolated, containers run non-root/read-only, and gVisor adds kernel isolation. So **every infrastructure-level unknown that a throwaway-cluster prototype can settle has now been settled, and all came back GO.** The one real design constraint that survived contact: **hibernation is a cold restart** — a suspended sandbox is deleted and resumed as a new pod, so the interactive design must rehydrate session-from-history and must NOT bank on prompt-cache warmth across hibernation (this reshapes the cost model, not the feasibility). Net: the interactive architecture is **buildable as specified**, with hibernation-cold-start as the one thing to design for up front. What remains is not prototype-settleable — it is **build-discipline and integration risk**: the concurrency kernel (R5), the agent↔control-plane versioning contract, and wiring the proven pieces together (runner-in-sandbox + Langfuse spans, PT-2's ~1 day of remaining glue). See §8 for the honest remaining-risk list.

## 8. Remaining major risks to implementation (post-prototype)

The infra unknowns are settled. What is left are the risks that a cluster prototype *cannot* retire — they are proven or retired only by building carefully and testing the integrated system. Ranked by how likely each is to hurt a first client build.

1. **The concurrency kernel (R5) — highest residual risk.** The per-thread routing lock, the finish-race compare-and-swap ("a message arriving as the run completes"), steer-vs-interrupt, and no-retry-after-side-effects are correctness logic, not infra. Prototypes don't settle them; a correct state machine + a soak test that runs concurrent threads with a mid-thread batch job and a sandbox-kill-mid-run does. This is the one lane (F1) to keep single-owner and adversarially review. **Now compounded by a proven fact:** since hibernation cold-restarts the pod (PT-1), the resume path must deterministically rehydrate session state from history and re-establish the `thread_ts → sandbox_id` route — the finish-race and the resume-rehydrate interact, and that interaction is exactly where duplicate side effects hide.

2. **The agent↔control-plane versioning contract — highest *operational* risk (and the strategy doc's own top red flag).** On-prem means "we don't know who upgraded which agent when." A backwards-compatible, versioned ACI + plugin-format contract with an explicit support window and an auto-upgrade story is a hard prerequisite for any leave-behind, and the team has no versioning-discipline muscle yet. De-risk by building `packages/aci-protocol` as a frozen, versioned interface first (the plan already sequences C1 first) and adding a contract-compat CI test. This is process risk, not code risk, which is why it is easy to under-fund.

3. **Prompt-cache economics under real hibernation (cost risk, now quantified).** PT-1+PT-2 together prove cache warmth exists *within* a continuous claim (turn-2 read 40k cached tokens) but is destroyed by suspend/resume. So the budget/cost model must assume cache-cold on every resume, and the warm-pool/hibernation TTL policy becomes a direct cost lever (hibernate too eagerly → every resume is full-price; hold sandboxes too long → idle compute cost). This needs a real cost model against expected thread cadence, and a gateway-passthrough cache check if LiteLLM is ever inserted (never put a content-based model router in front — it breaks the prefix cache; see the stored prompt-caching learning). Not a blocker, but a number to get right before pricing the leave-behind.

4. **Agent Sandbox is pre-1.0 (v0.5.0, no maturity label) and the warm-pool path is unproven.** The core `Sandbox` CRD works (PT-1), but `SandboxWarmPool`/`SandboxClaim`/`SandboxTemplate` are a separate extensions install not yet exercised, and the whole project can ship breaking API changes. Mitigation is architectural (the ACI abstraction + a plain-K8s-Job fallback for interactive threads), and one more prototype step: install the extensions and prove warm-pool claim latency + the claim→route wiring.

5. **OTel gen_ai semconv is still "Development" status.** Attribute names can change under us; PT-4 proved ingestion works today, but the durable UI schema must sit behind a thin mapping layer (the plan already says this). Low severity, but a silent-breakage source on dependency bumps — pin the semconv opt-in and own the mapping.

6. **On-prem substrate variance (two concrete, newly-evidenced instances).** (a) A customer CNI that does not enforce NetworkPolicy produces a *silent* security false-pass — the install must run the before/after egress probe as a gate. (b) ClickHouse now requires AVX and hard-crashes on older CPUs (PT-4 hit this on SSE4.2) — the chart needs a CPU-feature preflight and a pinned SSE4.2-compatible ClickHouse fallback. Both are "runs on their hardware" risks that only surface at a customer site; both are cheap to gate for.

7. **Production auth model (not the dev OAuth path).** PT-2 used Brian's OAuth token to prove the mechanism; that is not the shipping credential for a customer runner (personal identity, per-user rate limit, refresh/expiry). The leave-behind needs a proper API-key / Bedrock / Vertex path with per-tenant key management — straightforward, but it is real work that the OAuth shortcut did not exercise.

**What is NOT a remaining risk:** the runtime substrate (proven), the control endpoint (proven), steering/interrupt (proven), the trace/eval backbone and its footprint (proven), and the security rails (proven). The build can start on the walking skeleton with confidence in the foundation; the risk has moved from "will the pieces work" to "will we wire them correctly and operate the versioned contract with discipline."
