# 76. A closed, versioned attribute schema for the runner's OTel span stream

Date: 2026-07-23

Status: Accepted

Implements Stone 0 of [#512](https://github.com/curie-eng/agentos/issues/512)
("Epic: closed-typed telemetry schema and close the approval-gate
observability gap"). Extends [ADR-0004](0004-langfuse-observability-and-eval-backbone.md)
(Langfuse as the single observability backbone) and mirrors the versioning
discipline of [ADR-0036](0036-aci-semver-and-reader-policy.md) and the
committed-artifact-plus-CI-gate discipline of
[ADR-0074](0074-versioned-json-schemas-for-cli-results.md). Names the gap
[ADR-0038](0038-observability-cli-helper-for-the-agent-dev-loop.md) called out
as open ("did an approval get requested" is not answerable from Langfuse
today) and decides how Stones 1-5 close it, without reopening ADR-0038's
single-backend decision.

## Context

`runner/src/agentos_runner/otel.py` emits three span kinds (`agent.run`,
`llm.generation`, `execute_tool`) with a fixed but ungated set of attributes:
`langfuse.trace.name`, `langfuse.session.id`, `langfuse.user.id`,
`gen_ai.request.model`, `model`, `gen_ai.usage.{input,output,cache_read_input,
cache_creation_input}_tokens`, `gen_ai.tool.name`, `gen_ai.operation.name`, plus
resource attributes `service.name`, `agentos.session_id`, `agentos.sandbox_id`.
Every span attribute passes through `_set()` (otel.py:38-45), which routes the
value through `redact_span_attribute` (`redact.py`) — but `_set()` accepts any
string key. Nothing stops a new call site from attaching an arbitrary key, and
nothing validates the emitted stream at export time; `redact.py`'s own
docstring already names this epic's export-time validator as the second layer
its scrub complements but does not replace.

Separately, ADR-0038 named two capabilities the CLI's observability helper
cannot serve from Langfuse because the runner's span stream carries neither:
whether an approval was requested/resolved, and runner-local budget headroom.
Both are explicitly "an open item... not promised," and ADR-0038 warns that
reopening its Prometheus/single-backend decision (Decision 2) needs a
superseding ADR. Closing the approval-gate half of that gap is this epic's
stated goal; it must be done by adding to the existing gen_ai span stream, not
by introducing a second telemetry surface.

Review of xAI's Grok Build harness surfaced a portable discipline: a closed
enum of permitted attribute keys plus an export-time validator that drops any
record carrying an unlisted key or an unscrubbed secret. That discipline is
adopted here; Grok's metrics-only, traces-free shape is not — this repo keeps
its existing gen_ai span/trace model.

## Decision

**1. Closed attribute-key enum.** Every key `otel.py` may attach is declared
in a single closed registry (an enum or equivalent), covering the full set
named above unchanged, plus new members Stones 3/6 add for the approval
decision and budget headroom. `_set()` (or its replacement) takes a member of
this registry, not a bare string, so a new call site with an unlisted key is a
construction-time error, not a silent addition to the wire shape. This mirrors
ADR-0036's "strict producers" half of its reader-policy split: the runner is
the sole producer of these spans, so nothing needs a tolerant-consumer
allowance here.

**2. `schema.version`, versioned like ADR-0036's change classes.** The
registry carries a `schema.version` (starting `v1`), stamped as a resource
attribute alongside `service.name`. The compatibility policy mirrors
ADR-0036/ADR-0074 rather than inventing a new one:
   - **Additive, no bump:** a new optional attribute key.
   - **Breaking, version bump:** removing a key, renaming a key, or changing
     an existing key's value type.

   Unlike ADR-0074's schema (a wire contract validated against real output),
this schema is a producer-side allowlist; a version bump is a signal to
consumers of the exported stream (Langfuse-side tooling, the Stone 4 read
proxy) that the key set changed, not a compatibility gate enforced on read.

**3. Fail-closed export policy: drop the offending attribute, not the whole
span.** At export time (an OTel `SpanProcessor` hook, ahead of the OTLP
exporter), any attribute whose key is absent from the closed registry, or
whose value still matches one of `redact.py`'s `REDACTION_RULES` after the
existing scrub pass, is stripped from the span before export. The span itself
is still exported (with the offending attribute removed) rather than dropped
whole: a span missing one bad attribute is still useful trace data, whereas
discarding the whole span (or the whole batch) turns one bad key into a
bigger observability gap than the leak it prevents. This is a defense-in-depth
backstop, not the primary control — the primary control is Decision 1 making
a bad key un-constructible in the first place.

**4. Approval-decision emission seam: the worker threads the decision through
boot env; the runner stamps it on the resumed turn's span.** The resolved
`ApprovalStatus` (`approved`/`rejected`/`expired`, `apps/api/src/agentos_api/
models.py`) is known server-side, in the worker, before the resumed turn's
sandbox boots — `apps/worker/src/agentos_worker/kernel.py`'s `process_event()`
already reads it via `binding.py`'s `approval_grant_tool()` /
`approval_resumed_kind()` to build that turn's boot env. Stone 3 adds one more
boot-env value alongside the existing `AGENTOS_APPROVAL_GRANT_TOOL` /
`RESUMED_KIND_ENV` pattern, and `otel.py` stamps it as a new registry member
(e.g. `gen_ai.approval.decision`) on that turn's `agent.run` span. This is
chosen over having the runner-side `ApprovalGate` (`runner/src/agentos_runner/
approval.py`) emit it directly, because the worker is the party that actually
observes the terminal decision (including `expired`, from the SLA sweeper
path) — the runner only ever sees a grant or a block, not the three-way
status. Request-time coverage ("was an approval requested," independent of
its resolution) is out of scope for Stone 3; the boot-env seam only carries
resolved decisions on resume, so a request that is never resolved is not
covered by this ADR and would need its own emission point in `kernel.py`'s
`_pause_for_approval()` if wanted later.

## Consequences

- Stones 1, 2, and 5 have a concrete registry and versioning rule to build
  against instead of inventing one independently.
- Stone 3 has a decided emission seam (boot-env threading), so it does not
  need to relitigate runner-side vs. worker-side emission.
- The fail-closed export behavior (Decision 3) means a bug that produces an
  unlisted key degrades observability for that one attribute rather than
  failing a run or silently leaking a value — an explicit tradeoff of
  availability for safety, consistent with this being a credential-handling
  process.
- This does not reopen ADR-0038 Decision 2: no Prometheus, no second metrics
  backend, no change to Langfuse being the single authoritative read path.
- Request-time approval-gate observability ("was an approval ever requested,"
  independent of resolution) remains open; only the resolved-decision half of
  ADR-0038's named gap is closed here.
- Runner-local budget headroom (ADR-0038's other named gap) is deliberately
  out of scope for the required stones; Stone 6 may extend this same registry
  to cover it later, as stretch work.
