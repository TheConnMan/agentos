# 67. A `--model` sweep row with zero completed turns fails loudly instead of reporting 0%

Date: 2026-07-21

Status: Accepted

Extends [ADR-0022](0022-eval-completeness-tier-parity-and-trace-promotion.md)
(eval completeness) and sits alongside
[ADR-0055](0055-the-fake-model-is-a-plumbing-fixture.md) (the fake tier's
`plumbing_ok` third state); supersedes neither. Read
[ADR-0041](0041-every-verb-is-answered-at-every-tier.md) for the exit-class
boundary this ADR reuses (`Failure`, exit 1) and
[ADR-0053](0053-expected-terminal-status-in-eval-cases.md) for the completion
gate (`expect_status`) this decision reads without changing.

Closes [#622](https://github.com/curie-eng/agentos/issues/622), the fourth
acceptance criterion of [#526](https://github.com/curie-eng/agentos/issues/526)
("a model that is not resolvable fails loudly rather than silently landing in
the matrix's unlabelled column") that #526's closing commit did not actually
implement. Related to [#606](https://github.com/curie-eng/agentos/issues/606)
(a sealed sweep labelling fake rows with real model names) as the same family of
defect — a sweep reporting a comparison it did not perform — but not settled
together: #606 is the fake tier mislabelling a row; this is a real tier never
producing one.

## Context

`agentos <skill|local|cluster> eval --model` runs an eval suite once per named
model and reports a pass-rate per model (#526) so an operator can decide "can we
move this skill to a cheaper model." There was no validation anywhere in the
repo of whether a `--model` value names a model that actually resolves — and
building one was explicitly the wrong fix to reach for, because the CLI cannot
know the set of valid ids for a BYO model (#24, OpenRouter and native
Anthropic-format endpoints). The sweep has to tell a real failing model apart
from a typo'd one **using signal it already has**, not an allowlist.

Traced for `--model bogus-model-xyz`: the runner boots and passes its health
check (health never calls the model), so `wait_healthy` sees nothing wrong. The
first turn then hits the SDK's model error, which `runner/src/agentos_runner/session.py`
converts to a classified-failure `Final` **by design** — the ACI stream must
always terminate in a final, so a raised SDK/transport error becomes a
classified failure rather than a truncated, final-less stream. That design is
correct and this ADR does not touch it. HTTP still returns 200, so the CLI's
`send_event` sees success; `turn_passes`/`turn_outcome` (`cli/src/evals.rs`)
correctly mark the case `Fail`, because the turn never reached `done`. Every
case in the suite fails the same way, and the pre-#622 `report_sweep` rendered
that as `bogus-model-xyz 0/5 0%` and exited 0 — by the sweep's own stated
design, "a sweep is a comparison, not a gate, so it never exits non-zero on a
model that scored below 100%." A typo'd model is indistinguishable from a real
model that legitimately scored zero.

At local/cluster the failure mode was worse, not better: it was a misleading
20-interval timeout ("Check the worker eval consumer is running"). The
platform's eval-stream consumer (`apps/worker/src/agentos_worker/eval/stream.py`,
`_acquire_target`) provisions a fresh sandbox per triggered model; when
provisioning itself fails (an unbootable or unregistered id, a bad credential),
`_run_and_report` takes the `_report_failed` branch with an **empty** results
list before `run_eval_suite`/the Langfuse recorder ever runs — so literally
nothing is ever traced for that `(version, model)`. The CLI's poll loop
(`cli/src/message.rs::eval_sweep`) never sees the triggered sha's row land and
eventually times out, and the message it gave named only the eval consumer —
the wrong subsystem when the actual cause is a model that never resolved.

## Decision

**A model that produced zero completed turns across the whole suite is a
categorically distinct sweep outcome from a real (if unlucky) 0%, computed from
completion, not from any list of valid model ids.**

### 1. Completion is already a signal every tier has; it was just never carried past the per-case gate

Every case is already graded through a completion gate before the grader is
ever consulted (`turn_completed`/`_run_case`'s `expect_status` check): a case
either reaches a `final` matching its expected status, or it does not. What
#622 adds is *aggregating that gate's outcome per model*, across the whole
suite, instead of discarding it once the per-case `Fail`/`Pass` verdict is
computed:

- **Skill tier** (`cli/src/commands.rs::run_suite_cases`): now returns, plus the
  usual `(id, outcome, seconds, output)` rows, how many of the suite's cases
  `turn_completed` (now `pub` in `cli/src/evals.rs`). `CaseOutcome` alone cannot
  carry this: `turn_outcome` intentionally collapses "never completed" and
  "completed but graded wrong" into the same `Fail` for the per-case report,
  which is correct there and exactly the ambiguity a sweep row must not have.
- **Local/cluster tier**: the worker already wrote `error` on a graded FAIL's
  trace metadata precisely when the turn did not complete (a classified
  failure, the wrong terminal status, or a transport/runner exception),
  reserved and left empty for a turn that completed and simply lost on the
  grader (`EvalCaseResult.error`, unchanged by this ADR). `apps/api/src/agentos_api/evals.py`
  reads that pre-existing field to add a `completed` count to
  `EvalModelSummary`/the eval matrix API — no new instrumentation in the
  worker, only a new aggregate over data it already recorded.

A row's `total > 0 && completed == 0` is the new outcome; `SweepRow` (shared
Rust struct behind `cli/src/commands.rs::report_sweep`, the single point both
the skill and the local/cluster `--model` sweep converge on) exposes it as
`never_completed()`.

### 2. `report_sweep` renders the outcome distinctly and fails the whole sweep, but never calls `std::process::exit` itself

A never-completed row renders as `NEVER COMPLETED` (human table) or
`"never_completed": true, "pass_rate": null` (`--json`) rather than a
percentage — a comparison was never performed for that model, so no rate is
printed for it, fabricated or otherwise. `report_sweep` then returns `Err`
naming every offending model and the likely cause ("the model most likely
never resolved: a typo'd or unregistered id, a missing/invalid credential, or a
runner that never came up for it") instead of blaming the eval consumer, with a
fix hint to verify the model id/credential.

Returning `Err` — rather than calling `std::process::exit` the way
`report_eval` does for a genuine per-case failure — is deliberate: `report_sweep`
is called from both the skill-tier command (no cleanup owed) and the
local/cluster `message::eval_sweep`, which may be holding a live `kubectl
port-forward` guard for the whole poll. Exiting inside `report_sweep` would
skip that guard's `Drop` the same way #751 already fixed for the approval-resume
path. Returning `Err` lets ordinary `?`-propagation unwind through the caller,
dropping its guards, before `main.rs`'s centralized error handler exits the
process — `CliError::failure` (exit class `Failure`, exit 1; ADR-0021 decision
1) is the class, since this is a genuine runtime failure, not a usage error and
not a tier-absence.

### 3. The local/cluster timeout message is fixed to name the model and both plausible causes

Separately from the row-level outcome above, the pre-existing "no row landed at
all within the timeout" bail message is rewritten to name the still-pending
model(s) and state both causes plainly — the eval consumer not running, **or**
one of the pending models never resolving so its job never produced a single
trace — instead of pointing only at the eval consumer. This is the same
underlying gap (an unresolvable model given no signal to explain itself) showing
up as a different symptom at these two tiers, so it is fixed in the same change
even though it is not gated by `SweepRow`/`never_completed()` (nothing landed in
the matrix at all for this failure mode, so there is no row to compute
`completed` from — the fix here is wording, not a new count).

### 4. A real 0% is untouched

A model that completed every case and lost every grader check still reports
`0/N (0%)` and the sweep still exits 0 for it — `report_sweep`'s stated
contract ("a sweep is a comparison, not a gate") holds for every row except the
one this ADR carves out. No allowlist of model ids is introduced anywhere; the
distinction is entirely a function of completion, which is why it survives a
BYO model (#24) with no knowledge of what ids are valid.

## Consequences

- **A `--model` sweep that previously exited 0 on a fully-broken model now
  exits 1**, at all three tiers, for that specific case only (`total > 0`,
  `completed == 0` for at least one row). Any other sweep result — including a
  real 0% — is unaffected.
- **`EvalModelSummary` (`apps/api/src/agentos_api/schemas.py`,
  `cli/src/api.rs`) gains a `completed: int = 0` field**, backward-compatible
  under `#[serde(default)]`/a Pydantic default the same way `plumbing` already
  is; an API that predates the field reads as `completed = 0` for every row,
  which degrades toward the new "distinct outcome" reading rather than silently
  misreporting a pass rate. This mirrors an accepted, pre-existing skew class in
  this codebase (`plumbing`'s own default), not a new one.
- **No `GraderKind`, no `ExpectedStatus`, no allowlist.** The frozen eval-case
  format (ADR-0019) is untouched; this is a *result*-shape change (a new
  aggregate field on a summary type), not a case-shape change.
- **`#606` remains open and separate.** A sealed/fake sweep mislabelling rows
  with real model names is a different defect (a labelling problem on a
  non-graded row) from this one (a real-tier row that never completed); fixing
  both in one PR was considered and rejected as scope creep once it was clear
  neither shared an implementation once this ADR's mechanism (`SweepRow`,
  `completed`) was in place.

## Alternatives considered

- **A model allowlist checked before the sweep boots.** Rejected per the issue's
  own framing: it is brittle against BYO models (#24), where the CLI has no way
  to know the valid id set, and it would need to be kept in sync with every
  provider's catalog by hand.
- **Widen `CaseOutcome` with a fourth per-case variant (e.g. `NeverCompleted`).**
  Rejected: it would change the single-case report's shape
  (`eval_json`/`report_eval`, the schema-gated `cli/schema/eval.schema.json`)
  for a distinction that only matters in aggregate, across a whole suite run
  for one model. The per-case report already correctly calls a non-completed
  case `Fail` (ADR-0055's rule 2, "a fake turn that does not complete is still a
  failure," generalizes the same way for a real turn); the new signal belongs at
  the row level, not the case level.
- **Call `std::process::exit` directly inside `report_sweep`, matching
  `report_eval`.** Rejected: `report_sweep` is shared by the local/cluster path,
  which may be holding a live port-forward guard for the duration of the poll
  loop; exiting inside the shared function would skip that guard's `Drop` the
  way #751 already had to fix once for the approval-resume path. Returning `Err`
  keeps the guard-then-exit ordering structural.
