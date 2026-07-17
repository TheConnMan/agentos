# 53. Add an expected terminal status to the frozen eval-case format

Date: 2026-07-17

Status: Accepted

Extends [ADR-0019](0019-freeze-eval-case-format.md) (the frozen eval-case format
and its regenerate-and-diff drift gate) and composes with
[ADR-0010](0010-approval-gates-and-human-in-the-loop.md) (approval gates and the
`awaiting-approval` terminal status). Supersedes none.

## Context

An approval-gated turn does not end `done`. When an agent correctly refuses to
run a destructive action and instead posts an approval card, the turn parks at
`awaiting-approval` and waits for a human. The frozen eval-case format could not
express "the approval gate correctly blocked the destructive action" as a green
case, because the pass condition hardcoded `SessionStatus.DONE` (the Python
runner's `_run_case` gate and the CLI's `turn_passes`): a case passed only if the
turn reached `done` and the grader matched.

That hardcoding made scoring anti-correlated with safety. In the run-7
experiment, a match-anything grader over a turn that correctly blocked an issue
close scored RED (the turn ended `awaiting-approval`, not `done`), while the same
grader over a turn that merely narrated "I asked for approval" and then completed
scored GREEN (it ended `done`). An agent that correctly held the gate looked
worse than one that only talked about it. "The gate blocked the action" could
never be a passing eval case, so the single most important safety behavior was
unmeasurable.

## Decision

Add an optional `expect_status` field to the frozen `EvalCase`, typed as a new
`ExpectedStatus` enum with exactly two wire values, `done` and
`awaiting-approval`. It defaults to `done`, so every pre-existing case and every
existing behavior is byte-identical. A case passes iff
`final_status == expect_status` AND the grader matches the graded answer. Setting
`expect_status: awaiting-approval` makes "the gate held" a first-class green
assertion: pair it with a match-anything grader to assert purely on the gate
holding, or a real grader to also assert the narration text.

`ExpectedStatus` is a dedicated enum, not a reuse of the full `SessionStatus`
wire enum, so the eval-cases schema stays self-contained (no cross-package `$ref`
into `aci_protocol`). It is a deliberate subset and can be widened additively
later. The change moves all three artifacts of the frozen format together in one
reviewed change per the ADR-0019 mechanism: the Pydantic models are the source of
truth, `apps/worker/schema/eval-cases.schema.json` is the regenerated derivative,
and the Rust structs in `cli/src/evals.rs` are the hand-mirror kept honest by a
byte-level conformance test.

## Alternatives considered

- **Expose the `TrajectoryScorer` as a per-case grader that asserts the
  destructive tool was NOT called (#389).** Complementary, not a substitute, and
  tracked separately: it is a larger run-layer change (a per-case trajectory
  expectation on the frozen case) that answers a different question ("which tools
  ran") than the terminal-status assertion here ("did the gate hold"). This ADR
  ships the smaller, sufficient fix.
- **Reuse the full `SessionStatus` enum instead of a new subset.** Rejected: it
  would couple the eval schema to `aci_protocol` and admit `classified-failure`
  and `idle-awaiting-input` as "expected" statuses. A classified-failure is infra
  breakage and is never an eval success; idle-awaiting-input is out of scope. The
  dedicated subset keeps the schema honest about what an eval can assert.

## Consequences

- Existing cases are unchanged: the field defaults to `done`, and the schema does
  not add it to `EvalCase`'s required list.
- The enum is additively wideable: a new terminal status an eval could assert is
  a one-line addition to `ExpectedStatus`, guarded by the same drift gate.
- The local/cluster message path honors the same gate: `reply_passes` gates on
  `expect_status`, so `local message`/`cluster message` eval parity holds an
  `awaiting-approval` case green exactly when the turn parked awaiting approval.

Reference issue [#262](https://github.com/curie-eng/agentos/issues/262).
