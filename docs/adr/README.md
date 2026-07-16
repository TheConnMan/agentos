# Architecture Decision Records

Every architecture decision in AgentOS is recorded here as a numbered ADR. An ADR
captures **what was decided and when**, along with the reasoning and the
alternatives weighed at that time.

ADRs are immutable once Accepted. When the thinking changes you add a **new ADR
that supersedes** the old one rather than editing it, so the chain of
supersessions is the history of how intent shifted — not just a snapshot of the
current state.

## Claiming a number

**The next free number is one past the last row of the table below.** Read it
from this index, not from an `ls` of your branch: a branch that has not been
rebased shows a stale tail, and every number collision this repo has had came
from exactly that. Check open PRs for a number already reserved off-main before
claiming one.

The number-prefix uniqueness of `docs/adr/` is gated in CI (`scripts/check-docs.sh`),
so a collision fails the build rather than reaching main.

## The index

The table below is generated from the ADR files themselves by
`agentos_doclint` — number from each filename, title from its heading, status
read verbatim from its `Status:` line. **Do not hand-edit it.** Run
`bash scripts/check-docs.sh` to regenerate, then commit the result.

<!-- BEGIN GENERATED: adr-index (do not edit by hand; run scripts/check-docs.sh) -->
| # | Decision | Status |
|---|---|---|
| 0001 | [Record architecture decisions](0001-record-architecture-decisions.md) | Accepted |
| 0002 | [Kubernetes Agent Sandbox as the interactive runtime substrate](0002-kubernetes-agent-sandbox-as-runtime-substrate.md) | Accepted |
| 0003 | [Stateless-first sessions; rehydrate on resume; no cross-hibernation cache assumption](0003-stateless-first-rehydrate-on-resume.md) | Accepted |
| 0004 | [Langfuse as the single observability + eval backbone](0004-langfuse-observability-and-eval-backbone.md) | Accepted |
| 0005 | [claude-agent-sdk adapter behind a frozen ACI session contract](0005-claude-agent-sdk-adapter-and-frozen-aci.md) | Accepted |
| 0006 | [Security rails are chart defaults, not hardening backlog](0006-security-rails-as-chart-defaults.md) | Accepted |
| 0007 | [Adopt-not-build boundaries; build only five things](0007-adopt-not-build-boundaries.md) | Accepted |
| 0008 | [Multi-tenancy: one code path, pooled RLS, hard-siloed compute](0008-multi-tenancy.md) | Accepted |
| 0009 | [Per-agent secrets and connector credentials](0009-per-agent-connector-auth.md) | Accepted |
| 0010 | [Approval gates and human-in-the-loop](0010-approval-gates-and-human-in-the-loop.md) | Accepted |
| 0011 | [OpenCode as the second harness behind the ACI](0011-opencode-second-harness.md) | Proposed (acceptance gated on the steer spike below) |
| 0012 | [A substrate-agnostic worker and a channel-agnostic runner (the thin-shim thesis)](0012-substrate-and-channel-agnostic-core.md) | Accepted |
| 0013 | [Concurrency and delivery: at-least-once streams with an idempotent, side-effect-aware kernel](0013-concurrency-and-delivery-model.md) | Accepted |
| 0014 | [A git push is the deploy: immutable bundles, promote-not-rebuild, evals as a CI gate](0014-git-push-is-the-deploy.md) | Accepted |
| 0015 | [The credential plane: no broker, prefix-mapped, fail-loud and fail-closed](0015-credential-plane.md) | Accepted |
| 0016 | [Swappable jobs around an opinionated core; the second implementation teaches the interface](0016-swappable-jobs-around-an-opinionated-core.md) | Accepted |
| 0017 | [Tri-language frozen contracts: Pydantic as source of truth, generate the rest, gate on drift](0017-tri-language-contract-codegen.md) | Accepted |
| 0018 | [Greeting/help pre-model short-circuit in the kernel](0018-greeting-help-pre-model-short-circuit.md) | Accepted |
| 0019 | [Freeze the eval-case format: one schema, frozen in place under apps/worker](0019-freeze-eval-case-format.md) | Accepted |
| 0020 | [The message port: a rendering-free channel interface with capability negotiation](0020-message-port-rendering-free-channel-interface.md) | Accepted |
| 0021 | [AgentOS is a harness for coding agents: the CLI's primary user is Claude Code](0021-agentos-is-a-harness-for-coding-agents.md) | Accepted |
| 0022 | [Eval completeness: run the same evals at every tier, grade what actually happened, and promote real traces into cases](0022-eval-completeness-tier-parity-and-trace-promotion.md) | Accepted |
| 0023 | [Controller NetworkPolicy RBAC: cluster-scope read, namespace-scope mutate](0023-controller-networkpolicy-rbac-cluster-read-namespace-mutate.md) | Accepted |
| 0024 | [`cluster deploy` reaches the platform API via the UI `/api` proxy](0024-deploy-reaches-api-via-ui-proxy.md) | Accepted |
| 0025 | [The memory port and its first loader: a scoped namespace over the durable state store](0025-memory-port-and-first-loader.md) | Accepted |
| 0026 | [Extract the storage port now; defer the second (GCS/Azure) adapter](0026-extract-storage-port-defer-second-adapter.md) | Accepted |
| 0027 | [A thin broker port at the non-sacred seams; defer the second broker](0027-thin-broker-port-defer-second-broker.md) | Accepted |
| 0028 | [The sandbox substrate is a resilience-only fallback, not a product swap axis](0028-substrate-is-resilience-fallback-not-product-swap-axis.md) | Accepted |
| 0029 | [The conversation-history port and its first loader: transcript replay over the durable state store](0029-conversation-history-port-and-first-loader.md) | Accepted |
| 0030 | [A proactive within-episode memory agent over the frozen ACI injection seam](0030-proactive-within-episode-memory-agent.md) | Proposed |
| 0031 | [Harness-neutral runner seams for the second harness](0031-harness-neutral-runner-seams.md) | Proposed |
| 0032 | [Explicit named-provider egress, resolved at install](0032-explicit-provider-egress.md) | Accepted |
| 0033 | [Scoped, least-privilege sandbox state token](0033-scoped-sandbox-state-token.md) | Accepted |
| 0034 | [Approval authorizers resolve membership in the API](0034-approval-authorizers-resolve-membership-in-the-api.md) | Accepted |
| 0035 | [One-shot post-approval allowance at resume boot](0035-one-shot-post-approval-allowance.md) | Accepted |
| 0036 | [ACI semver, reader-policy asymmetry, and a wire-lock gate that enforces it](0036-aci-semver-and-reader-policy.md) | Accepted |
| 0037 | [Opt-in binding hook and Pareto model routing](0037-opt-in-binding-hook-and-pareto-model-routing.md) | Accepted |
| 0038 | [An observability CLI helper for the agent-dev loop](0038-observability-cli-helper-for-the-agent-dev-loop.md) | Accepted |
| 0039 | [Bounded delivery: a delivery cap and a dead-letter graveyard](0039-bounded-delivery-and-a-dead-letter-graveyard.md) | Accepted |
| 0040 | [Adopt the Agent Client Protocol as an edge projection](0040-adopt-acp-as-an-edge-projection.md) | Proposed |
| 0041 | [Every verb is answered at every tier](0041-every-verb-is-answered-at-every-tier.md) | Accepted |
| 0042 | [Adopt LLM-as-a-Verifier: a continuous-score semantic grader and a live progress signal](0042-llm-as-a-verifier-grader-and-progress-signal.md) | Proposed |
| 0043 | [The interface catalog is generated from declared front-matter and gated in CI](0043-generated-interface-catalog-and-doc-lint-gate.md) | Accepted |
| 0044 | [Workflow-controlled agents run on AgentOS as a callable substrate first, a unified plane only on demand](0044-workflow-controlled-agents-callable-substrate.md) | Proposed |
| 0045 | [The status line is the mutable part of an immutable ADR](0045-the-status-line-is-the-mutable-part-of-an-immutable-adr.md) | Accepted |
| 0046 | [Converged approval gates: durable provenance, loud route resolution, and observe-only resume reconciliation](0046-converged-approval-gates-and-durable-provenance.md) | Accepted |
<!-- END GENERATED: adr-index -->
