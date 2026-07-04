# Curie AgentOS: Product Direction for the Pivot

Discussion material for the team ideation meeting. Goal of the meeting: set a product goal. This doc is the synthesis Brian brings in.

Sources: May 28 Curie Product Meeting (Ashish), May 20 Ashish 1:1, the May 18 agent-native redesign note, the on-prem deployment research brief, the agent-first product thesis, the a16z "Palantirization of Everything" article, and a fresh code survey of the platform (agents, portal/API, observability) as of June 1 2026.

## The one-sentence frame

The pivot is from "coding agent for MuleSoft" to "bespoke agent development company" (Ashish, May 28). The **Curie AgentOS** is the opinionated platform boundary that keeps that pivot from collapsing into "Accenture for agents." It is the internal product we build so that forward-deployed agent projects are *assembled from primitives*, not hand-written per customer, and so every bespoke build feeds a compounding platform.

Andrusko's pressure test is the discipline we hold ourselves to: *"Where does the shared product end and customer-specific code begin, and how fast is that boundary moving?"* The AgentOS is our answer to that question.

## Why this is the right frame (grounding in what was said)

- Ashish, May 28, named the model directly: *"Companies that have done this very successfully are companies like c3.ai, palantir... they would leave behind their platform in the customer's environment... our image is running on eks on kubernetes."* He confirmed Brian's read back: *"the palantirization of things... that strong platform grows and grows because we're aggregating all of these agentification from everybody into a platform."*
- The leave-behind is explicit recurring revenue + lock-in: *"some monitoring components that we'll have to build and leave behind so that there is a reason for them to come back to us... those leave behind components could be skills or tools we have encoded in a way that they can't get to without us."*
- Ashish's focus statement: *"Our focus is more on the intelligence of the agent... developing the right context and the right tooling to make that agent work. And it could be in any of these frameworks."* (LangChain, LangGraph, CrewAI, Google ADK named.)
- The production gap is the white space: *"two major things... not everybody knows how to do well. One is evals and second is eval's memory management, state management and observability and model drifts. The moment a demo toy agent hits production it falls apart."* The company that can run agents in production wins. **That is an AgentOS claim, not an agent claim.**

## Do we actually have an edge? (the honest evaluation)

Brian's gating question: *"If we're just like n8n, it's not going to work. Do we have an edge, or are we hoping to have an edge?"* The honest answer is sharp and uncomfortable: **we have a real, measured edge in exactly one place, and a hoped-for edge everywhere the pivot wants to go.**

**Where the edge is REAL (measured, in the code, has customers):** integration migration (MuleSoft / Boomi / TIBCO / IBM ACE → Mule4 / Boomi / Spring / Camel / Azure).
- The verification loop is genuine and pervasive: 26 files under `assistants/mule` drive an MUnit / `mvn test` cycle — the agent generates code, compiles it, runs tests, feeds failures back, and iterates. That is a *deterministic verification oracle* for this domain. A horizontal agent (Claude Code, Cursor) can write plausible MuleSoft code; it cannot compile-and-MUnit-verify it inside a domain sandbox the way Curie does.
- The external benchmark backs this: per ScarfBench (cited in `operational-logic-moat`), five SOTA agents capped at 15.3% pass on cross-framework enterprise Java refactoring, and 1 of 204 attempts produced a fully equivalent migration. Horizontal agents genuinely fail at this. (Caveat: this number is second-hand from the wiki and should be re-verified before quoting externally.)
- Real production customers and revenue (Optus etc.), plus the DataWeave agent's community virality. This is **not n8n** — n8n is deterministic glue with no codegen and no verification; Curie's migration product is codegen wrapped in a domain compiler/test oracle.

**Where the edge is HOPED-FOR (not yet real — the honest gap):** "bespoke agents for any back-office process" (Agentify: finance ops, procurement, support).
- There is **zero** finance/AP/procurement/PO agent code in the repo today (a grep for invoice/accounts_payable/procurement/purchase_order returns only Mule/Boomi/Azure files). We have built none of it.
- The verification oracle **does not transfer**. For migration, "verified" means compiles + MUnit passes + equivalent to source. For AP invoice matching, "verified" means "does this invoice reconcile against the PO and the contract" — a business-rule oracle Curie has neither built nor has a corpus for. The moat has to be **rebuilt per vertical**; it starts at zero each time.
- Brian's own thinking already names this: the eval/verification corpus is *"the named gap... without it, 'verified' is a marketing word."* There is no first-class eval corpus or eval harness in the codebase today (the incidental "eval" hits are library code, not a Curie harness). So the generalizable claim — *"we can put agents in production when others can't, because we do evals/state/observability/drift"* (Ashish's white-space argument) — is plausible and partly supported by the genuinely strong instrumentation that exists, but it has been **proven only in the integration vertical.** Extending it to finance ops is a hypothesis, not a track record.

**The n8n risk is real, and it is about where the value sits, not about any one mechanism.** The differentiation was never the orchestration/config layer (commodity: n8n, LangGraph, Modal, CrewAI); a graph of LLM prompts with no domain verification oracle underneath is n8n-with-LLMs, only non-deterministic. The differentiation is the **verification oracle + operational-logic corpus**, which is per-vertical and today exists only for integration. The genuinely hard problem worth solving is not "how do we describe an agent" but the **iteration-vs-deployment-vs-reproducibility tension**: when agents and code ship continuously, it is very hard to say "*this* version of the agent ran *this* job and produced *this* result," which is the precondition for any eval. That is a solved problem elsewhere (LLM eval/observability tooling, experiment trackers, prompt registries) — we should borrow a pattern rather than invent or overconstrain ourselves into a DAG-as-a-service. The declarative-definition idea is one option; the reproducibility requirement is the actual constraint.

**What this means for the pivot (the actionable read):**
- The defensible identity *today* is "the verification-grade integration/migration agent company." That is real and has customers. Smart Shift and Curie Agents sit on it.
- "AgentOS for any process" is a bet that the *production-discipline scaffolding* generalizes (the instrumentation, the verify-loop pattern, the run-in-customer-VPC plumbing genuinely are reusable). But the *value* per new vertical comes from a new verification oracle + corpus that starts at zero. So the moat compounds **within** a vertical, not automatically across unrelated ones.
- Therefore the vertical-selection rule is the whole game: pick verticals where (a) a deterministic or semi-deterministic verification oracle can be built, and (b) the stakes/volume justify building it. **Finance-ops AP/invoice matching fits** — there is a clean reconciliation oracle (does it match the PO/contract?), which is why it is the smart first Agentify pick. **Customer support (the General Mills lead) is weaker** — "did the agent resolve the ticket well" has no clean oracle, so it is closer to the n8n trap; flag it.
- The single best leading indicator to watch: **can we build a working verification oracle for the first non-integration vertical (finance AP)?** If yes, the thesis generalizes and the AgentOS bet is earned. If we cannot, we are an integration company that should stay one and say no to the rest.

**What the prod usage data actually shows (Prometheus, last 30 days) — and it sharpens the verdict.**
- **The product today is the human chat web app, not an agent-native or batch-migration platform.** `/api/conversations` saw 1.67M requests; the MCP surface saw ~858 (and that is poll-inflated, so a handful of real jobs) — roughly 0.05% of API traffic. The agent-native/MCP pivot is **not happening organically yet**; the leading indicator the May-18 thesis wanted to see is sitting on the floor.
- **Interactive vs batch is ~5:1.** `RunAgent` (chat) = 1,861 gRPC calls; `SubmitAgentTask` (async migration/coding jobs) = 374 (~12/day).
- **Token burn is dominated by chat/Q&A/landing-page agents** (`landing_page_asst` ~598M tokens, `unknown` ~56M), with the actual coders far smaller (`flex_gateway_rust_policy_coder` ~14M, `azure_logic_apps_coder` ~1.8M). The heaviest compute is pre-sales / test-drive / KB Q&A, not paid production migration.
- **It is a Mule-only business in prod.** Agent runs by connection: MULESOFT 511, BOOMI 3, Spring/Azure ~0. The diversification (Boomi/Spring/Azure/Smart Shift) is nascent in code and effectively absent in production usage.

Honest reading: the proven edge (verified Mule migration) is **real but running at low production volume right now** (~12 batch jobs/day, Mule-only), and the broader agent/MCP usage that the pivot is betting on is barely registering. By usage, today's CurieTech looks more like an AI-assisted MuleSoft Q&A / test-drive web app than a high-volume verified-migration factory. That does not invalidate the edge — it means the pivot is a bet on volume and breadth the current telemetry has not yet demonstrated, and the indicators to watch (MCP growth, SubmitAgentTask growth, non-Mule connection types) are all near floor.

Remaining caveats: (1) 30-day window — migration work is bursty/project-shaped, so a concentrated Optus-scale engagement outside this window would be missed, and each `SubmitAgentTask` may be a large multi-flow job; (2) I could **not** measure migration *quality* (first-attempt-pass, semantic-equivalence win rate) from Prometheus, because `agent_run_duration` is only instrumented for the chat QA agent — which is itself evidence that the eval/diagnostics layer this pivot depends on is not built. Closing that gap is exactly Phase 0 (config-versioning-for-evals).

## The three-layer architecture (this is the whole frame)

The product splits cleanly into three layers. Most of the meeting's confusion will come from conflating them; keep them separate.

### Layer 1 — The Platform (control plane / portal / API)

This is "users, credits, which agents you have, billing." It is what `packages/api` already is. It stays **Curie-hosted SaaS** in every topology.

What we have today (strong):
- Multi-tenant: `organizations` (workspace) under `billing_accounts`, `user_orgs` membership, `tenants` table with Cognito + Okta/Google/SAML/Microsoft SSO.
- Credits/billing just hardened into a clean **prepay model** (subscription credits + purchased credits, strict `total_balance <= 0` enforcement, `is_billing_period` now derived not stored). Stripe wired with webhook-cached tables.
- `usage_events` with **per-MCP-origin and per-API-key attribution** (claude-code/cursor/codex tagged on every row). Super-admin can already partition spend into mcp / chat / internal channels.
- API tokens (`sk-cur-...`, SHA-256 hashed, org-scoped, scoped permissions) and an MCP server at `/mcp` with capability gating via `CURIE_ENABLED_API_CAPABILITIES` / `CURIE_ENABLED_MCP_CAPABILITIES`.

The gap that matters most for the pivot:
- **There is no per-org "agent entitlements" table.** "Which agents you have" is gated today by (a) which deployment you point at, (b) env-var capability flags on that deployment, (c) whether you have credits. That is fine for one SaaS product; it is the missing primitive for "different customers get different agent bundles" and for "promote a bespoke agent to a first-class catalog agent other customers can buy." **Build this.**

### Layer 2 — Agent Orchestration (data plane / runtime)

This is where on-prem shows up. The orchestration layer runs agents either on Curie k8s or in the customer VPC. The control plane (Layer 1) stays with Curie either way.

What we have today:
- gRPC `AgentService` (`SubmitAgentTask` writes a DB task row, returns `task_id`; a `DatabasePoller` in the agent pod claims it; `handler.py` dispatches via a 500-line `match cmd_type` block to `agent.run_task`).
- **The forward-deployed seam already exists.** `should_use_runner()` (`utils.py:1304`) plus `ENABLE_SAAS_RUNNER` plus the `curie-runners` StatefulSet (a Go binary that claims `RunnerTasks` and runs maven near the work). Today it offloads maven; it generalizes to "run the whole agent in the customer VPC and report metadata home."
- Per-command timeouts already model long jobs (`MIGRATION_ACCELERATOR` = 48h).

The topology we build toward (the on-prem brief's "Option B"): **agent image in the customer k8s + Curie control plane.** Ashish's stated preference. Customer data never leaves; the agent reports completion metadata over outbound HTTPS. The migration agent is the most plausible first unit (scored 2/5 on decoupling today — shared Postgres, shared S3, Vertex embeddings, Secrets Manager are the couplings to break). Full-platform-on-prem (Option A) is deferred. The agent↔control-plane API underneath this is critical path and is covered under "what platform do we have" (d) and the red flag below.

**The hard operational truth under Layer 2 (the biggest red flag).** Today the team owns a monorepo and ships API + agents together, rolled out at the same time — so the agent↔API contract is never actually a contract, it's just current `main`. On-prem breaks that completely: we will not know who upgraded which agent when. This forces discipline the team does not have yet — a versioned, backwards-compatible agent↔control-plane API, an explicit support window (how many agent versions do we support against a given control-plane version?), and an auto-upgrade story for the leave-behind agent. **None of the rest of this works without that contract discipline.** It should be treated as a first-class workstream, not an afterthought, and it is the single most likely place the operationalization cracks.

### Layer 3 — Base Agents + the Agent SDK

The agents themselves and the sandboxed runtime. This is where "the intelligence" lives, and where the copy-paste-able base agents must be built.

What we have today (~20 agents across `mule/`, `migration/`, `spring_integration/`, `generic/`, `techspec/`):
- Two base hierarchies: an **inner LLM loop** (`Asst` -> `GeneralLLMBasedAsst` -> `SimpleFnCallingAsst`, `max_steps=150`, heartbeat, parallel tool exec, model fallback, history compression) and an **outer task agent** (`CodingAssistant`, `QAAssistant`, `RefactorAgent`).
- A clean cloning pattern *already exists*: `SpringIntegrationRefactorAgent` subclasses `RefactorAgent` and overrides only `_build_coder` — ~500 LoC to add a whole new target platform. The verify loop (generate -> write -> `mvn`/MUnit -> feed errors back) is shared.

The gaps that block "strong base agents you copy-paste":
- Parallel duplication: `spec_base.py`, `task_manager.py`, `internal_prompts.py` are copied per platform rather than parameterized.
- No agent registry — adding a command means editing the 500-line `handler.py` match block.
- Platform-specific tools are duplicated (`tools/platform/mule/` vs `tools/platform/spring_integration/`).

**Runtime image vs. declarative agent definition (the "ship config, not Docker images" question).** A design spike against the code says the single-step LLM loop is already ~60-70% data-drivable: `SimpleFnCallingAsst.__init__` takes a flat, serializable signature (`assistant_name`, `assistant_sys_msg`, `tools`, `model_id`, `max_steps`, `temp`, `is_exit_fn`...; `simple_fn_calling_asst.py:60`), `AsstType.get_asst` already dispatches on a string asst-type, and `_BOOMI_ASST_CONFIG` (`handler.py:163`) is literally a list of config dicts. The prompts are giant Python string modules (`migration/prompts.py` is 5,298 lines) — data masquerading as code. So the *runtime* (toolchain, the LLM loop, model-fallback ladders, history compression, the compile/MUnit verifier) stays baked in the image; the *agent definition* (DAG of steps + prompts + tool references + model params + eval fixtures) could ship as versioned config. What genuinely can't be config: new tool implementations and the imperative multi-phase logic (RefactorAgent's `FilesystemGraph` phases, seed-assembly guards, per-API parallelism) — those are still code ships.

The honest verdict on this, and the phased path:
- **Do NOT build a generic declarative-DAG engine, and do NOT adopt LangGraph/CrewAI wholesale.** That is commodity agent-infra (LangGraph/Modal/E2B already exist) where Curie has no edge, and the agents that would most benefit are exactly the ones whose value is imperative phase logic that resists config. Building a DAG runtime to iterate faster on a Boomi prompt is yak-shaving.
- **Phase 0 — version-to-result reproducibility for evals (ship now, table stakes).** This is the genuinely valuable half and the actual constraint: being able to say "*this* agent version ran *this* job and produced *this* result" when agents and code ship continuously. Add an immutable `agent_definitions(id, config_hash, definition_json, created_at)` table and stamp `config_hash` (sha256 of the *materialized* prompt+tools+model+max_steps+git_sha) onto every task at **run start, not lookup time**, mirroring the existing `metric_metadata->>'task_calc_version'` precedent (`usage_events.py:243`). This is a solved problem in the broader ecosystem (LLM eval/observability tooling, experiment trackers, prompt registries) — borrow the pattern rather than invent. It works *today* even while agents stay code-defined, and it does not require committing to any declarative-agent direction.
- **Phase 1 — externalize prompts to versioned config.** Iterating with a client becomes "edit prompt, bump hash," no image ship. Captures most of the "ship configuration" goal cheaply.
- **Phase 2 — a thin Curie DSL for single-step / simple-chain agents only.** Deserialized into the existing `AsstType.get_asst`. Build it only when a second framework target or a genuinely branchy/resumable agent forces it — not speculatively.

## Answering the meeting's core questions

### What platform do we have?
A mature SaaS control plane (Layer 1), a working gRPC orchestration layer with a forward-deployed seam already in place (Layer 2), ~20 production agents on two reusable base hierarchies (Layer 3), and a rich-but-AWS-coupled observability stack. The bones of an AgentOS exist. What is missing is (a) the agent-entitlement primitive, (b) a clean Agent SDK that removes the per-platform duplication, (c) an on-prem-portable observability path, and (d) a hardened public agent↔control-plane API.

On (d): on-prem clients will want different runtimes and may host on a different cloud entirely, so the API and the agents must be able to communicate over the **public internet**, authenticated by token / API key. Today the API↔agents path is effectively an internal API (same monorepo, shipped together). Turning it into a robust, token-authed, versioned public surface is critical path for the whole pivot — it is the load-bearing dependency behind every forward-deployed topology, and it is coupled to the versioning red flag called out under Layer 2.

### How could we run agents on a platform?
The runner seam is the answer. Generalize `curie-runners` from "maven offload" to "claim-and-run-a-whole-agent." For Curie-hosted, agents run in our `core` namespace as today. For forward-deployed, ship the agent image + a thin control-plane client into customer k8s; it long-polls Curie for tasks, runs against local repos/data, and reports redacted metadata home. Framework-agnostic at this boundary: the orchestration layer launches a *container*, so the agent inside can be Curie-native or LangGraph/CrewAI/ADK.

### How are the current agents structured, and what does a strong base agent look like?
Covered in Layer 3. The recommendation: keep the Curie-native inner loop as the default base (it already has verification, fallback, and step-bounding baked in — the things that make agents survive production), but invest in an **Agent SDK** that turns the implicit `RefactorAgent` subclass pattern into an explicit contract: subclass a base coder/QA/refactor agent, register a `CommandType` in a registry (not the match block), supply platform prompts + tools + **eval fixtures**, and inherit observability + billing + runner-dispatch for free. Frame each agent on Brian's three-layer agent architecture (immutable identity doc + compiled learnings + human approval gate) and the V3 Planner/Actioner/Executor/Evaluator decomposition into idempotent primitives.

### What does the discovery -> build -> deploy -> generalize pipeline look like?
Ashish's services shape: **paid Assessment (compressed to ~1 week vs the industry's ~1 month) -> Implementation/Deploy -> Maintenance (leave-behind = recurring revenue).** The migration assessment agent (input = source repo -> assessment) is the template; for Agentify, discovery is messier (interview the humans whose job is being automated, digitize, feed through an assessment agent).

The generalization step is the load-bearing one and maps to Ramp's "Generalize our work" / Palantir's "recycle bespoke into templates each quarter." Concretely, a bespoke agent built for customer X gets promoted to a first-class catalog agent when it (1) passes the shared eval-corpus bar, (2) is parameterized not hardcoded, (3) gets an entitlement flag so other orgs can subscribe. **This is exactly why the entitlement primitive in Layer 1 is on the critical path** — without it there is no "first-class citizen available to other customers."

### Consistent logging, instrumentation, observability — especially on-prem
This is the part Brian flagged as the big problem, and it is also the highest-leverage product opportunity, because **the observability leave-behind IS the monitoring component Ashish wants to leave behind** — the lock-in, the recurring-revenue hook, and the "discovery of new agents to build" signal, all in one.

What we have (strong, do not rebuild): rich per-agent-run and per-LLM-call metrics (`AGENT_RUN_DURATION`, `LLM_CALL_DURATION`, `LLM_FALLBACK_TOTAL` with classified reasons, token counts by type, cache-miss reasons, inter-message latency), OTel traces with PII redaction, structured JSON logs with a per-request `rid` context var, Loki structured metadata. This directly covers Ashish's named production-gap (evals, state, observability, drift).

What breaks on-prem (the gap to close):
- Everything defaults to `*.monitoring.svc.cluster.local` + AWS S3 + Secrets Manager. `OTLP_ENDPOINT` is the *only* redirection hook, and on-prem with no endpoint set **silently disables tracing** (`api/tracing.py:343-350`) — a footgun.
- No phone-home collector, no Prometheus remote-write, Loki always needs S3, and logs carry no `trace_id` so you cannot pivot trace<->log directly.

Recommendation: standardize on an **OTel Collector as the single telemetry egress point** in the customer VPC. The agent emits OTLP + JSON-logs-to-stdout + `/metrics`; the customer-side collector either keeps everything local (their Grafana) or forwards a redacted, aggregated subset back to Curie. Make this collector the productized leave-behind. Fix the silent-tracing-disable footgun and add `trace_id` to log lines as table stakes.

## The agent build-and-iterate lifecycle (and what the system must do to support it)

Designing and building an agent is not a single deploy — it is a long, high-iteration loop: discover → assess → build an initial agent → have it run real jobs → evaluate how well it did → diagnose failures → adjust → redeploy → repeat, and only then graduate to production. The system has to be built *for* that loop, because most of the calendar time and almost all of the risk live in the iterate-and-evaluate middle, not the initial build. This is the operational muscle Ashish says nobody has and that is the actual white space.

What the loop looks like, and the system requirement each stage imposes:

1. **Discovery.** Interview the humans whose work is being automated, gather and digitize examples of the workflow and its edge cases. *Open question (Ashish has not answered): who actually runs these interviews and designs — FDE, Rachna's team, a solutions role?* This is a critical staffing dependency, not a platform feature, but it gates everything downstream. System requirement: a place to capture and version the gathered intent/examples as the agent's spec and as the seed of its eval set.
2. **Assessment.** Paid, compressed to ~1 week (vs the industry's ~1 month). The migration assessment agent (input = source repo → assessment) is the template; for Agentify the input is the digitized discovery. System requirement: assessment is itself an agent run, so it needs the same instrumentation and config-versioning as everything else.
3. **Initial build.** Assemble from base agents + the SDK where possible; write only the genuinely novel tools/logic. System requirement: the Agent SDK and base-agent catalog (Layer 3) so this is assembly, not greenfield.
4. **Run jobs + evaluate.** The agent runs real jobs; we measure how well it did against the eval set. **This is the stage the current system does not support well.** You cannot judge whether an agent performed well unless you know exactly which version of the agent produced each result — which is why config-versioning-for-evals (the `agent_definitions` hash stamped at run time, Layer 3 Phase 0) is a hard prerequisite, not a nice-to-have. System requirement: every job stamped with a config hash; an eval harness that scores runs against fixtures; a way to see results sliced by agent version.
5. **Diagnose + iterate.** Failures get root-caused; prompts/tools/config get adjusted; the agent is redeployed. This must be *fast* — ship configuration, not Docker images (Layer 3), so a client-facing iteration is minutes, not a release. System requirement: hot-swappable versioned config + per-run diagnostics (traces, tool-call timelines, the verifier output) good enough to tell *why* a run failed.
6. **Deploy + maintain.** Promote to production; the leave-behind monitoring component (Layer 2 observability) provides the ongoing signal and the recurring-revenue/discovery hook.

The throughline: **diagnostics, config-versioning, and eval attribution are the load-bearing platform investments**, because the build-iterate-evaluate loop is where agents are actually made to work, and it is exactly the loop the current ship-the-monorepo model does not serve. Generalization (custom → first-class catalog agent) happens at the *end* of this loop, when an agent has earned its eval scores and been parameterized.

## Proposed product goal (to land at end of meeting)

> **Build the Curie AgentOS:** an opinionated internal platform with (1) a catalog of strong base agents + an Agent SDK that makes a new agent a subclass-plus-config, (2) a framework-agnostic orchestration + runtime layer that runs an agent identically on Curie cloud or in a customer VPC, and (3) a control plane that owns entitlements, billing, and an observability leave-behind. Success = a new bespoke agent goes from signed assessment to production in a bounded number of weeks, and any bespoke agent can be promoted into the catalog for other customers to buy.

First proofs to commit to (so it is not abstract):
1. **Build a working verification oracle for one non-integration vertical** — finance-ops AP/invoice matching (Ashish's named target, and it has a clean reconciliation oracle). This is the proof that the edge generalizes beyond migration; if we can't build the oracle, the "any process" thesis is not yet earned.
2. **Generalize one existing agent into the catalog** — the cleanest candidate is the migration/Spring-Integration line, since the `RefactorAgent` subclass pattern already proves the mechanism.
3. **Ship the load-bearing platform pieces:** config-versioning-for-evals (`agent_definitions` hash stamped at run time), the agent-entitlement primitive, the hardened public token-auth agent↔control-plane API, and the observability collector leave-behind. These are the dependencies everything else sits on.

North-star metric that enforces the Andrusko discipline: **percent of each engagement assembled from primitives vs. hand-written bespoke.** If that number is not climbing engagement over engagement, we are becoming Accenture-for-agents and the platform spine is not forming.

## Decisions to force in the meeting

1. **Framework stance.** Curie-native base agents as the default, framework-agnostic only at the container/orchestration boundary, and config-as-definition limited to versioning-for-evals now + prompt-externalization next (no generic DAG engine)? (Recommended.) Or invest in a declarative agent runtime sooner (more flexible, but that is commodity infra where we have no edge)?
2. **First Agentify vertical — chosen by oracle, not by lead.** Finance ops (AP matching) has a clean verification oracle; the General Mills support case does not. Pick the vertical where we can actually build a verifier, or accept we are doing n8n-grade work there.
3. **On-prem topology.** Commit to the agent-in-VPC + Curie control plane topology as the one we build toward; defer full-platform-on-prem; treat the public token-auth agent↔control-plane API as critical path regardless.
4. **Fund the contract-discipline workstream now?** Versioned, backwards-compatible agent↔control-plane API + support-window policy + auto-upgrade. This is the biggest operational red flag and a hard prerequisite for any on-prem agent. Yes/no this quarter.
5. **Build the entitlement layer + config-versioning now?** Both are on the critical path — entitlements for generalization and differentiated bundles, config-versioning for any eval to be meaningful. Yes/no this sprint.
6. **Observability leave-behind as product.** Fund the OTel-collector leave-behind as a first-class deliverable (it is the monitoring component + lock-in Ashish described), not as ops cleanup.

## The risk to keep on the table

CT is a ~7-person org with one new SDR and Brian at partial retainer. Andrusko's "what breaks if you sign 50 customers next year?" is unanswered. The AgentOS is precisely the bet that lets a small team take forward-deployed work without drowning — but only if the boundary actually moves. The first two engagements will be mostly custom (Ashish: *"the first few engagements will be more custom, but the next few should be less and less custom"*). The discipline is to treat that as a countdown, not a permanent state.
