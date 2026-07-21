# 69. The policy-gate resume reconciliation stays observe-only

Date: 2026-07-21

Status: Accepted

Implements [#559](https://github.com/curie-eng/agentos/issues/559).
Records the enforce/observe decision that
[ADR-0046](0046-converged-approval-gates-and-durable-provenance.md) §3
deferred to #559 ("ships observe-only and earns the data for a later enforce
decision"). It does not change any behavior established there; it decides,
with reasons, not to promote — and sharpens why, beyond the data gap ADR-0046
anticipated.

## Context

ADR-0046 §3 shipped the policy-gate resume reconciliation observe-only: when a
resumed policy-gate turn has gates armed, records no permission-gate block, and
ends clean with no side-effecting tool, the runner emits a warning frame
(`APPROVAL_NOT_ACTED_CLASSIFICATION`) and leaves the final clean
(`runner/src/agentos_runner/session.py`, `_approval_not_acted_lines`). ADR-0046
named its signal (`side_effect_emitted`) a proxy for "some tool ran," not "the
approved action ran," with two accepted failure modes: a false alarm on the
common text-only path, and a false pass when an incidental non-allowlisted tool
suppresses the warning. It deferred the promote/observe decision to #559,
gated on real false-alarm/false-pass rates from traces.

#559 asks for that decision, and its first acceptance criterion is observed
rates "from real traces."

## Decision

**Keep the reconciliation observe-only. Do not promote it to a terminal
status.** Revisit only post-release, and only against the two conditions in
Consequences.

The `side_effect_emitted` signal and the pinned known-limitation tests from
#544/ADR-0046 are left exactly as they are; nothing is re-targeted, because
nothing is promoted.

## Reasoning

Three findings, in increasing order of how decisive they are.

1. **The acceptance criterion's data cannot exist yet.** AgentOS is
   pre-release; there is no production gated-agent traffic to sample
   false-alarm and false-pass rates from. Waiting on that data is not a
   near-term path to a decision — it is a block until release. In its place we
   rely on something stronger than sampled rates for the failure modes
   themselves: they are provable **by construction**, confirmed in the current
   code. A text-only reply makes no tool call, so `side_effect_emitted` is
   always False and the warning always fires (false alarm on the primary
   legitimate use); any incidental non-allowlisted tool flips it True and always
   suppresses the warning (false pass). These are certainties, not frequencies.

2. **The "stronger signal" ADR-0046/#559 gesture at is not an observe-only
   tweak.** #559 suggests "binding the check to the specific gated tool rather
   than the blunt `side_effect_emitted` proxy." But on the resume turn the
   runner does not know which tool was approved: the resume marker is
   deliberately **authority-free** (`AGENTOS_APPROVAL_RESUMED_KIND=policy`,
   carrying no tool, by the #430/#544 security design so a model-authored
   request can never mint a bypass — ADR-0046 §3 contrasts it explicitly with
   the authority-conferring `AGENTOS_APPROVAL_GRANT_TOOL`). `TurnState` also
   records only a tool-call *count*, not tool names. Binding the check to the
   approved tool therefore requires extending the authority-free boot-env marker
   to carry the manifest-derived granted tool, threading it through the worker
   binding, and tracking per-tool call identity in the runner — a
   security-sensitive change spanning the runner and worker single-owner
   modules. That is exactly the kind of change #559 says to *decide* on before
   building, not to slip in under an instrumentation ticket.

3. **Even a precise signal would not justify enforcement, because approval is
   permission, not obligation.** Suppose the runner did know the exact approved
   tool and could see it was never called this turn. That still does not mean
   the turn failed: an agent approved to call `create_deal` may legitimately
   resume, reconsider, and decline — "on reflection this does not qualify,
   replying instead." "Approved tool not called" is intrinsically ambiguous
   between a real dropped-action bug and a legitimate decision to not exercise
   granted permission. A terminal hard-fail would therefore still cry wolf on a
   legitimate path — the alert-fatigue failure ADR-0046 rejected a day-one loud
   final for — and this residual is **semantic, not a signal-quality gap that
   more data or a better proxy closes.** This is the finding that turns "not
   yet" into "not on the current model of the control."

## Consequences

- The observe-only warning and its `APPROVAL_NOT_ACTED_CLASSIFICATION` frame
  remain in place as instrumentation. Operators and traces still get the
  signal; it just never gates a terminal status.
- The #544/ADR-0046 pinned known-limitation tests stay green as-is
  (`runner/tests/test_approval.py`). #559's AC3 ("if promoted, re-target the
  pinned tests") is not triggered.
- **#559 stays open as the post-release revisit tracker**, pinned to this ADR.
  Two things would reopen the decision, and both are post-release: (a) real
  false-alarm rates from live gated-agent traffic, which may still argue for a
  quieter or better-scoped *observe* signal even if not for enforcement; and
  (b) if the authority-free-marker extension in finding 2 is ever built as its
  own security-reviewed change, re-evaluate whether "granted tool not called"
  is precise enough to enforce — while carrying forward finding 3, which the
  extension does not resolve.
- No code changes ship with this ADR. It is a decision record; the
  behavior it decides about already exists and is unchanged.

## Alternatives considered

- **Promote to a loud terminal status now.** Rejected: #559 explicitly forbids
  it without the data, the data cannot exist pre-release, and findings 1 and 3
  show the current signal false-alarms by construction on the primary
  legitimate path. Shipping it would train operators to ignore a security
  control the first time it matters.
- **Build the authority-free-marker extension and enforce on the precise
  signal, in this change.** Rejected here as out of scope and premature: it is
  a security-sensitive cross-module change (finding 2) that #559 says to decide
  before building, and finding 3 shows even the precise signal does not cleanly
  enforce. If pursued post-release it warrants its own ADR and review, not a
  ride-along on the observe/enforce decision.
- **Close #559 as won't-do.** Rejected: the underlying question (does a
  resumed approval that never acts deserve a louder signal?) is legitimate and
  worth revisiting with real traffic. Recording "keep observing, revisit
  post-release against named conditions" preserves it without pretending a
  decision the data cannot yet support has been made.
