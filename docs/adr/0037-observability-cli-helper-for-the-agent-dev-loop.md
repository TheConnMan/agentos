# 37. An observability CLI helper for the agent-dev loop

Date: 2026-07-16
Status: Proposed

Proposes the agent-facing observability query surface for the CLI. Research and
proposal only; it ships no code. Extends
[ADR-0021](0021-agentos-is-a-harness-for-coding-agents.md) (the CLI's primary
user is a coding agent) and [ADR-0004](0004-langfuse-observability-and-eval-backbone.md)
(Langfuse as the observability backbone), and reuses the thin-client-over-api
pattern of [ADR-0024](0024-deploy-reaches-api-via-ui-proxy.md). Implements the
research ticket [#461](https://github.com/curie-eng/agentos/issues/461); the
mechanical `--open` split of the current human verb is the separate
[#460](https://github.com/curie-eng/agentos/issues/460).

## Context

Per ADR-0021, the CLI's primary user is a coding agent, not a human. During the
build-and-test loop an agent runs a bundle, then needs to ask what just
happened: did my tool calls fire, what did that run cost, is my error rate
climbing, show me trace `X`. It must get those answers as structured data
through the CLI, not by opening a browser tab it cannot read.

Today's `agentos local observability` is the wrong shape for that consumer. The
handler prints two hardcoded localhost URLs (`http://localhost:28080/?api=1` and
`http://localhost:23000`) and shells `xdg-open`/`open` at them best-effort
(`cli/src/commands.rs:1547-1577`). It takes no arguments, fetches no data, and
returns nothing an agent can branch on. It is a browser launcher, so there is
not even anything for the global `--json` flag to structure. The current handler
is itself the anti-pattern ADR-0021 warns about: it only ever calls the human
emitters (`ui.kv`/`ui.payload`) and never builds a JSON payload. Issue #460
mechanically splits the "open a browser" behavior out of this human verb; this
ADR is the separate question of what the agent-facing query surface should be,
and it deliberately does not ship it (Proposed, for human review first).

### Research 1: what an agent needs during the build/test loop

Ground the answer in what the runner actually emits, not a generic dashboard
feature list. The runner produces a fixed three-span tree per turn
(`runner/src/agentos_runner/otel.py:1-8`): a root `agent.run` (SpanKind.SERVER)
carrying `langfuse.trace.name`, a child `llm.generation` holding
`gen_ai.request.model` and the `gen_ai.usage.{input,output,cache_read_input,cache_creation_input}_tokens`
counts, and one `execute_tool` child per tool call stamped with
`gen_ai.tool.name` and `gen_ai.operation.name`. That span shape is the
authoritative list of what is answerable. It maps cleanly to the agent's real
questions: cost and token usage of a run (from `gen_ai.usage.*` and Langfuse's
derived cost), whether and which tools fired (the `execute_tool` children),
latency (span timing), recent failures and error rate (observation `level`), and
a specific trace by id.

One item on the ticket's pressure-test list, "did an approval get requested," is
not in this span stream. Approvals are an ACI concern handled by the worker and
authorizer (ADR-0034/0035), and the runner's OTel span tree does not emit an
approval-request span. So the honest answer is that approval-gate observability
is a gap this helper cannot serve from Langfuse today; it is called out as an
open item, not promised. (ADR-0022 separately proposes capturing approval-gate
frames for evals from the ACI wire, a different pipeline from these OTel spans.)
"Budget headroom," another item on the ticket's pressure-test list, is the same
kind of gap: runner-local budget enforcement exists but is not surfaced in the
OTel span stream, so it is not answerable from Langfuse either and is likewise an
open item rather than a promise.

### Research 2: the tooling already in place to query

Langfuse is the single backend, and `apps/api` already proxies its read API on
two paths, so the query logic exists server-side and needs no reimplementation.

- **Per-run detail.** `apps/api/src/agentos_api/langfuse.py` is a thin async
  client over Langfuse's public read API; `build_tree` reconstructs the tool-call
  tree from the flat observation list via `parentObservationId`
  (`langfuse.py:96-130`). The routes are `GET /langfuse/traces` (list, optional
  `agent_id`) and `GET /langfuse/traces/{trace_id}` (single trace plus the
  reconstructed observation tree and `sandbox_id`), in
  `apps/api/src/agentos_api/routers/runs.py:26-52`.
- **Aggregates.** `apps/api/src/agentos_api/metrics.py` builds Langfuse Metrics
  API queries for five metrics: `runs`, `latency_p95_ms`, `tokens`, `cost_usd`
  (`metrics.py:35-46`), plus `error_rate` derived from an observations `level`
  query. These are served at `GET /observability/metrics/summary` and
  `GET /observability/metrics/series` (`apps/api/src/agentos_api/routers/observability.py`),
  filterable by `environment` and `agent`.

`apps/api/CLAUDE.md` states the invariant plainly: these observability endpoints
are read-only proxies, not new stores; Langfuse and the cluster are the source
of truth. A CLI helper is a third client of the same proxy the UI Runs and
Metrics views already consume.

### Research 3: backend authority, and the ticket's federation premise

This is the key correction to the ticket's framing. The ticket asks whether the
helper should federate "Langfuse API vs Prometheus" because "they answer
different questions." **There is no Prometheus path for runner data.** Its
absence is deliberate and documented in two places: `apps/ui/CLAUDE.md:28-33` and
`apps/ui/README.md:87-94` both record that the Metrics tab intentionally diverges
from the design canon's PromQL surface because "the real API is Langfuse
aggregates with no Prometheus." The aggregate numbers come from Langfuse's
Metrics API, not PromQL (`metrics.py`). Even the OTel collector self-reports no
Prometheus metrics.

So the premise does not hold today: Langfuse is the one authoritative backend,
reached by two read paths (per-run traces/observations vs Metrics-API
aggregates). The helper does not federate across two backends because there is
only one; it picks the right Langfuse read path per question. Prior art
reinforces this: no surveyed tool merges trace and metrics data into a single
query schema, and the consistent pattern is separate query paths per concern
(the AWS OpenSearch "unified observability" writeup, and Traceloop's
opentelemetry-mcp-server exposing `search_traces`/`get_trace` separately from
`get_llm_usage`; see prior-art section 3). Here those separate concerns map to
two Langfuse read paths rather than two stores. If a Prometheus aggregate path is
ever introduced, the same separate-verb pattern extends to it without changing
this decision.

### Research 4: tier applicability

Correcting the ticket's worry. Skill tier does not export to a "wrong Langfuse
project"; it exports nothing by default. `agentos skill up`'s `--otel-endpoint`
is a plain optional flag with no default value (`cli/src/main.rs`,
`SkillAction::Up`, `otel_endpoint: Option<String>`), threaded to the container
only `if let Some(endpoint)` (`cli/src/docker.rs:112-115`). Since the runner's
tracer is a no-op whenever the endpoint is unset
(`runner/src/agentos_runner/otel.py:48-49`), a plain `agentos skill up` with no
flags produces no traces at all, anywhere. Skill tier only exports when the
operator explicitly wires `--otel-endpoint` onto a reachable collector network,
and even then it has no `apps/api` to read them back through.

Local and cluster both auto-wire the collector: compose defines an
`otel-collector` service, and the chart sets the sandbox pod's
`OTEL_EXPORTER_OTLP_ENDPOINT` at the release's collector under
`otelCollector.deploy` (`charts/agentos/templates/agent-sandbox.yaml:432-437`).
Both tiers run an `apps/api` that reaches a Langfuse. So local and cluster can
answer the observability query fully; skill tier structurally cannot in the
default loop.

The tier-parity rule (ADR-0022's principle that a verb is answered at every tier,
implemented or with an honest unavailable) dictates the resolution: the helper
must still respond at skill tier with an explicit, machine-readable "not
available at this tier (skill runs do not export traces by default; wire
`--otel-endpoint` to a reachable collector to enable)" and a semantic exit code,
rather than crashing or hanging. ADR-0022's `skill eval`-only asymmetry (no
`local eval`/`cluster eval` yet) is the precedent for a verb whose tier coverage
is uneven and must be made honest rather than hidden.

### Research 5: auth and scoping

Name the real gap rather than paper over it. `apps/api` authenticates every
observability router with one shared platform API key (`require_api_key`,
`apps/api/CLAUDE.md`), and reads Langfuse through one shared project keypair
constructed once for the whole process (`langfuse.py`). Critically,
`GET /langfuse/traces/{trace_id}` performs no ownership check: it validates only
that `trace_id` matches a safe id charset, then fetches and returns the trace
(`runs.py:36-52`). Any holder of the platform key can read any agent's trace by
id.

The only "per-agent" scoping is a client-side trace-name substring match,
`agent_trace_filter` returning `agent-{agent_id}` (`metrics.py:23-33`), applied
by the list endpoint as an ergonomic filter and explicitly not a security
boundary. Recommendation: the helper scopes to the current agent by that
trace-name convention for ergonomics, and the ADR records plainly that this is
convenience, not isolation. A holder of the platform key reads all agents'
traces. True per-agent read scoping is a separate concern (a future ADR
analogous to the authorizer-identity work in ADR-0034); this helper must not be
blocked on it and must not claim isolation it lacks.

### Research 6: prior art, briefly

- **langsmith-cli** (langchain-ai/langsmith-cli) is the closest named prior art,
  self-described as "Built for AI coding agents." Command groups `trace`/`run`,
  `--format=json`, time-window filters (`--since`, `--last-n-minutes`), and
  graceful API-version fallback so an agent caller need not track version skew.
- **langsmith-fetch** offers a compact `raw` single-line JSON mode distinct from
  pretty JSON, explicitly for `jq` piping by a code agent.
- **Braintrust** `bt sql "..." --json` is a real SQL-over-logs query CLI (the
  generic-DSL shape weighed and rejected below).
- The **Datadog MCP** writeup is the best lessons-learned source: default field
  trimming, prefer aggregation over raw dumps (measured ~40% cheaper in agent
  tokens because the agent states its question instead of aggregating
  in-context), and error messages that name the fix inline ("unknown field
  'stauts', did you mean 'status'?").
- **ccusage** is the cautionary tale: Claude Code shipped a human-only `/cost`,
  so the agent-facing usage surface had to be rebuilt externally by parsing raw
  JSONL. AgentOS shipping the machine surface itself, day one, is the direct
  argument for this ADR.
- Cross-tool gap: none of the surveyed tools offers a first-class `--latest` /
  "my last run" shortcut, yet that is exactly the agent's real question. AgentOS
  should fill it (prior-art section 4).

## Decision

Replace the browser-launcher shape with an agent-facing observability query
surface. Concretely:

1. **Ship purpose-built query verbs, not a browser opener and not a generic query
   DSL.** A small family of thin-client verbs, each mapping to a real agent-dev
   question. The proposed shape (names open to review): a `runs` list supporting
   a first-class `--latest`, a `run <trace-id>` detail, and a `metrics` summary.
   The command namespace (a top-level `observability` noun versus `local`/`cluster`
   subcommands mirroring the existing verb) is left to the implementing issue.
   Each is a thin client over the existing `apps/api` Langfuse read proxy, the
   same thin-client-over-api model `cluster deploy` uses (ADR-0024). The query
   and analysis logic stays in `apps/api` where it already lives; the CLI does
   not reimplement Langfuse-query logic in Rust (ADR-0022's one-place-for-logic
   principle).

2. **Langfuse is the single authoritative backend; the helper picks the read
   path, it does not federate.** Per-run questions hit the traces/observations
   path; aggregate questions hit the Metrics API path. There is no second backend
   to merge, and no merged trace-plus-metrics schema is invented.

3. **Answer at every tier.** Implement at local and cluster, which have an
   `apps/api` reaching a Langfuse. At skill tier, which has neither an `apps/api`
   nor guaranteed export, return an explicit machine-readable "unavailable at
   this tier" payload with a semantic exit code and the fix hint (wire
   `--otel-endpoint`). No verb silently missing at a tier.

4. **Honor the ADR-0021 contract.** Under the global `--json` flag, each verb
   emits its structured result to stdout and human text to stderr, and returns
   the semantic exit codes (`0` success, `1` failure, `2` usage, `3` transient)
   defined in `cli/src/exit.rs:1-31`, with the `{"error","fix"}` error payload on
   failure. Proposed success shapes:

   - single run:
     `{trace_id, name, timestamp, model, cost_usd, tokens:{input,output,cache_read,cache_creation}, latency_ms, error, tool_calls:[names]}`
   - aggregates: the five-metric summary object (`runs`, `latency_p95_ms`,
     `tokens`, `cost_usd`, `error_rate`).

   Fields are trimmed to what the loop needs (Datadog's field-trimming lesson);
   a raw/full mode can be added later.

5. **Scope to the current agent by the trace-name convention for ergonomics, and
   record the read gap.** The helper filters to `agent-{agent_id}` for
   convenience only. It is not isolation: a platform-key holder can read every
   agent's traces. Fixing that is a deferred, separate concern (a future
   observability-read-scoping ADR), and this helper is explicitly not blocked on
   it.

## Alternatives considered

- **A generic query DSL / SQL-over-traces verb (Braintrust `bt sql` style).**
  Rejected as the primary shape. It over-serves the loop and forces the agent to
  author a query language to ask "what did my last run cost." Purpose-built verbs
  answer the actual questions at lower token cost (Datadog's
  aggregation-over-dumps result). A raw escape hatch could be added later without
  reopening this decision.
- **Keep it a URL printer and let #460 just split `--open`.** Rejected. A URL an
  agent cannot consume is the wrong shape for the primary user (the ticket's
  premise). #460 is the correct mechanical cleanup of the human verb, but it does
  not give the agent a queryable surface, which is what this ADR is for.
- **Ship an MCP server instead of CLI verbs (Traceloop
  opentelemetry-mcp-server style).** Rejected as the primary surface, for the same
  reason ADR-0021 rejected MCP-only: the CLI is the one entry point this repo
  commits to and it composes with the shell, git, and file muscle the agent
  already has. An MCP wrapper over the same `apps/api` endpoints can come later.
- **Federate a new Prometheus aggregate path alongside Langfuse.** Rejected as
  premature. No Prometheus for runner data exists today, and the divergence from
  the design canon is a documented, deliberate choice (`apps/ui/CLAUDE.md:28-33`).
  There is no prior art for a merged trace-plus-metrics schema. If a metrics
  backend is ever introduced, add a separate verb for it then.

## Consequences

- **This ADR adds no command by itself.** Like ADR-0021, it is a design decision
  whose commitments land as separate issues that reference it. The verb names,
  exact JSON fields, and flag surface are settled in the implementing issue and
  PR, against the constraints decided here.
- **Implementation is a thin CLI client plus, at most, small `apps/api`
  additions.** The read logic already exists (`langfuse.py`, `metrics.py`,
  `runs.py`, `observability.py`); the CLI work is calling those endpoints and
  emitting the `--json` shapes. A `--latest` shortcut likely needs no new endpoint:
  the existing `GET /langfuse/traces` list can be called with `limit=1` and the
  first result taken, so it is probably a zero-code server-side path.
- **Tier parity becomes a checklist/CI implication.** Every new observability
  verb must answer at all three tiers (implemented, or an honest unavailable with
  an exit code), the same rung-completeness discipline ADR-0022 applies to evals.
- **The shared-key read gap is a named, deferred limitation.** The helper reads
  through the one platform key with no per-agent ownership check on trace reads
  (`runs.py:36-52`); it scopes by trace-name convention for ergonomics only. True
  per-agent observability read scoping is future work (a dedicated ADR, analogous
  to ADR-0034), tracked separately and not solved here.
- **Approval-gate observability is an acknowledged gap.** The runner's OTel span
  tree does not carry approval-request spans, so "did an approval get requested"
  is not answerable from this surface; it depends on the separate ACI-frame
  capture ADR-0022 proposes.
- **Adjacent work.** #460 is the mechanical `--open` split of the existing human
  verb; #461 tracks this research and proposal. The two are complementary: #460
  cleans up the human affordance, this ADR defines the agent affordance.
