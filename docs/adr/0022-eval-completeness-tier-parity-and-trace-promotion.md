# 22. Eval completeness: run the same evals at every tier, grade what actually happened, and promote real traces into cases

Date: 2026-07-13
Status: Proposed

Gives the scattered eval work one decision spine. Extends
[ADR-0004](0004-langfuse-observability-and-eval-backbone.md) (Langfuse as the
observability + eval backbone) and [ADR-0019](0019-freeze-eval-case-format.md)
(the frozen eval-case format); it supersedes neither. It is the ordering and
decision reference for epic [#26](https://github.com/curie-eng/agentos/issues/26)
and the eval issues under it. Read
[ADR-0021](0021-agentos-is-a-harness-for-coding-agents.md) first: evals are the
contract the harness's parity promise is measured against.

## Context

The parity thesis (ADR-0021) is that **the same bundle plus the same
`evals/cases.json` stay green from `skill` to `local` to `cluster`**. Evals are
the contract that proves parity: a tier-to-tier divergence is the harness catching
a real environment bug, not the agent's skill logic. For that contract to mean
anything, two things must be true, and today neither fully is.

**Put plainly: as a parity contract, the eval system is broken today — it does not
prove what AgentOS advertises it proves.** The cold-start dogfood (run 2) makes this
concrete and is worth stating outright. That agent *did* drive the same bundle to
5/5 green at all three tiers — but the green was **hand-assembled around the eval
system, not produced by it.** To assert the deterministic engine actually ran, the
agent had to invent a tamper-proof "proof token" and grep the final text for it,
because no grader can check that a tool was ever called (axis 2 below). To "run the
same evals" at `local` and `cluster`, it had to hand-drive every case through
`message` and grep the reply itself, because those tiers have no `eval` verb (axis 1
below). A reader who sees "5/5 parity at every tier" concludes the contract works;
in fact a skilled agent manually reconstructed what the eval system could not do on
its own. The green proves the **runner's** parity, not the **eval system's** — and
on the fake-model CI path the same suite greens without a single tool call
([#337](https://github.com/curie-eng/agentos/issues/337)), so a green there proves
even less. Closing the gap between what a green eval *appears* to prove and what it
*actually* proves is the whole purpose of this ADR.

**1. You cannot actually run the same evals at every tier.** The CLI exposes
`skill eval` only (`cli/src/main.rs`, dispatched to `commands::eval`); there is no
`local eval` or `cluster eval` verb. The platform runs suites as fanned-out Jobs
per version, but a developer at the CLI can grade a bundle in the in-process runner
and nowhere else. The "same evals everywhere" promise has one of its three rungs
missing ([#344](https://github.com/curie-eng/agentos/issues/344)). The grading
*discipline* is already shared — the platform runner and the CLI both apply the
`Done`-gate and classified-failure gate before grading
(`apps/worker/src/agentos_worker/eval/runner.py`, mirrored by the CLI's
`turn_passes`) — so the foundation for identical grading exists; the execution
surface does not.

**2. The graders cannot assert what actually matters.** The frozen grader
taxonomy is `exact | contains | regex`, and all three run against the turn's
**final answer text** only (`apps/worker/src/agentos_worker/eval/models.py`,
`GraderKind`). A grader can confirm a version-shaped string appears in the reply;
it cannot confirm the deterministic engine or tool that was *supposed* to produce
that string was ever **called**, or that it returned a **structured result**. This
is not hypothetical. In a cold-start dogfood (run 2, a "release-radar" agent on
GLM-5.2), the agent had to *invent* a tamper-proof "proof token" that its engine
emitted and then grep the final text for it — a pure workaround for the absence of
a "a tool was called / a structured result is present" grader. Worse, the fake
model greens every text grader without calling a single tool
([#337](https://github.com/curie-eng/agentos/issues/337)), so a
green-on-fake result is false confidence: it proves the harness plumbing, not the
agent. The trajectory is *already on the ACI wire* — the runner emits a
`tool_note` (carrying the tool name) and a `side_effect_flag` for every tool use
(`runner/src/agentos_runner/translate.py`) — but the eval runner discards those
frames and forwards only `final.text` to the grader. We are grading a keyhole view
of a run we can already see in full.

**3. There is no path from a real run to a committed eval.** The single most
valuable eval cases are the ones reality writes: a production thread someone liked,
or one that caught a regression. Today, turning such a Langfuse trace into a
committed `evals/cases.json` case is entirely manual — read the trace, retype the
input, guess a `contains` string. Epic #26 names this the headline capability
("from traces to test suites") and it does not exist. This is the future
requirement that most shapes the design: it must be *easy* — ideally one command —
to promote an observed run into a curated, committed case.

This ADR is not new machinery invented from scratch. ADR-0004 already chose
Langfuse as the trace + eval store and flagged per-run version reproducibility as
"lands with the eval lane" — this is that lane. ADR-0019 already froze the case
*format* and deliberately left the door open ("pluggable scorers via `GraderKind`,
per-model matrices") — this ADR walks through that door. What has been missing is a
single decision that says how the pieces fit and in what order they land.

## Decision

Evals are AgentOS's parity contract. We make that contract complete along three
axes — **it runs everywhere, it grades the whole run, and it is fed by reality** —
and we sequence the work so the highest-leverage, lowest-risk pieces land first.

### 1. Tier-parity execution: one suite, one grader, three rungs

The same `evals/cases.json` runs at `skill`, `local`, and `cluster` through **one
shared grader implementation**, invoked identically at every tier. Add the two
missing CLI verbs, `local eval` and `cluster eval`
([#344](https://github.com/curie-eng/agentos/issues/344)), as thin drivers that
target, respectively, the compose stack's runner and a deployed release's runner —
mirroring how `message` already exists at all three tiers. A case grades identically
everywhere or the harness has a bug.

The grader is authored **once, in Python**, as the source of truth
([ADR-0017](0017-tri-language-contract-codegen.md)): the CLI does not
re-implement grading logic in Rust. Two mechanisms are on the table and the choice
is deferred to the phase that builds it, but the *constraint* is decided now — a
case must never grade differently at two tiers:

- **Preferred: grade server-side, at the runner.** The runner already owns the
  trajectory (it emits the `tool_note`/`side_effect_flag`/`final` frames). Move
  grading behind the `eval_case` ACI channel so the runner returns a graded result,
  and every tier's CLI verb becomes a dumb "submit case, print verdict" client.
  This makes cross-tier drift structurally impossible because there is exactly one
  grader. It requires the `eval_case` result frame to carry the graded outcome and
  the trajectory it graded — an ACI/frozen-contract change that stops and escalates
  per ADR-0005/0017, not a side channel.
- **Fallback: keep the CLI's hand-mirrored grader**, extended to the new taxonomy,
  and hold it honest with the existing byte-level conformance fixture (ADR-0019's
  mechanism). Cheaper, but every new grader kind is a second implementation to keep
  in lockstep.

Whichever is chosen, the invariant is: **the grader that decides pass/fail is
defined in one place and the tiers differ only in where the runner runs.** The
`Done`-gate/classified-failure parity already established stays as-is.

### 2. Grader taxonomy: assert the trajectory, not just the last sentence

Extend `GraderKind` beyond text matchers. The taxonomy grows to cover what a real
eval needs to protect, and the model — not the code — is what this ADR fixes:

- **Text matchers** (`exact | contains | regex`) — unchanged, still on final text.
- **Tool-call assertion** — a named tool was invoked during the turn (e.g. the
  bundle's deterministic engine ran). This directly closes the dogfood workaround
  and the #337 fake-model false-green: a tool-call grader **fails on the fake
  model**, which is the correct signal, because the fake never calls a tool. The
  raw material already exists (`tool_note` carries the tool name); the eval runner
  must capture those frames into the graded record instead of dropping them.
- **Structured-result assertion** — a tool returned a result matching a shape
  (present / non-empty / JSON-path predicate). This is what the dogfood's
  hand-rolled "proof token" was faking. It requires the tool result to reach the
  graded record, which is a larger contract touch than tool-call and is scoped to a
  later phase.
- **Trajectory / turn matcher** — an ordered or unordered set of tool calls
  occurred, and specifically **an approval gate was requested**
  ([#262](https://github.com/curie-eng/agentos/issues/262)). Gate-exercising evals
  are how we prove a human-in-the-loop bundle actually pauses for approval
  ([ADR-0010](0010-approval-gates-and-human-in-the-loop.md)) rather than just
  emitting agreeable text. `side_effect_flag` and the approval-request frames are
  the observable signals.

Composition (a case asserts *both* "tool X was called" *and* "the final text
contains Y") is in scope; the exact combinator is a schema detail for the building
phase, deliberately not designed here. Deny-by-default (ADR-0019) is preserved: a
case still must name at least one grader.

**The intent this taxonomy encodes must survive the handoff to whoever implements
it.** The failure mode is real and observed: a context-less implementer, handed
"write a tool-call grader," takes the mechanically-easiest path that satisfies the
words — e.g. grepping the final text for the tool's name — and produces a green test
that never inspects the trajectory. The assertion is about *what the run did*, read
from the observed trajectory frames, **not** about what the final text says the run
did. That distinction is the whole point of this axis.

### 3. Trace → eval promotion: reality writes the best cases

A single blessed command promotes an observed run into a committed case:

```
agentos eval add-from-trace <trace-id> [--assert tool:<name>] [--assert contains:<text>]
```

It reads the Langfuse trace (ADR-0004's store, addressed by trace id), and
**captures**:

- **the input** — the user turn(s) that opened the run, verbatim, so the case
  replays the real prompt rather than a paraphrase;
- **the trajectory** — the ordered tool calls and any approval gates the run
  exercised, so the promoted case can assert on behavior, not just the final text;
- **a graded assertion** — proposed from the trajectory (e.g. "tool `X` was called
  and the final text contains `Y`"), which the human **curates** before commit. The
  command writes a case into `evals/cases.json` in the frozen ADR-0019 shape; it
  does not silently commit — the developer (or their coding agent, per ADR-0021)
  reviews and edits the assertion, because a captured trajectory is a *candidate*,
  not a truth.

Provenance is recorded on the promoted case: the originating trace id is stamped so
a case can be traced back to the run it came from
([#266](https://github.com/curie-eng/agentos/issues/266)). A guided,
interview-style variant of this flow — walking a human through which assertions to
keep — is [#260](https://github.com/curie-eng/agentos/issues/260) and layers on top
of the same capture pipeline; per ADR-0021 that interview is agent-conducted, and
the CLI stays the non-interactive `add-from-trace` mechanism underneath. This
promotion pipeline is the spine of epic #26.

### 4. Multi-sample / variance-aware grading

Real model runs are non-deterministic; a single sample is a coin flip reported as a
fact. Grading gains an optional sample count and a pass rule
([#332](https://github.com/curie-eng/agentos/issues/332)): run a case *k* times and
pass on a threshold (majority, or pass@k / all-of-k for a flake-intolerant gate).
The default stays **1** so today's suites and the fake-model CI path are unchanged
and cheap; multi-sample is opt-in per case or per run, because it multiplies cost
and latency. The per-case result rollup (`EvalRunResult`) already records per-case
rows; variance grading extends it to carry the sample distribution, not just a
boolean. This axis is decided in principle here and scheduled after the taxonomy,
since a variance gate over a text-only grader is far less useful than one over a
trajectory grader.

### 5. Pluggable scorer seam and BYO-model matrix

- **Pluggable scorer seam** ([#261](https://github.com/curie-eng/agentos/issues/261)):
  the grader taxonomy above is deliberately a *closed, deterministic* enum, because
  reproducibility is the point of eval-as-CI (ADR-0004, ADR-0019). The extension
  point for open-ended scorers (an LLM-judge, a custom Python scorer) is a **named
  seam above the frozen grader port**, not a new `GraderKind` value. A scorer plugs
  in by name; the frozen case schema stays closed and reproducible. This keeps the
  ADR-0019 freeze intact while giving #261 a home. Concrete scorer implementations
  are out of scope for this ADR.
- **BYO-model eval matrix** ([#255](https://github.com/curie-eng/agentos/issues/255)):
  the run-result types are already keyed by `version`; the eval matrix is the natural
  place to add a **model** dimension so the same suite grades across BYO models and
  the results compare side by side. This is an orthogonal axis to grading and
  **deferred**: it is a reporting/fan-out concern that composes cleanly with
  everything above once tier-parity execution and the richer taxonomy exist. It also
  connects to the cross-harness parity evals
  ([#313](https://github.com/curie-eng/agentos/issues/313)) and the harness eval
  delta ([#326](https://github.com/curie-eng/agentos/issues/326)), both of which are
  matrix consumers, not new grading machinery.

### 6. Phased plan

Each phase maps to the issues it closes or advances, so this ADR is their ordering
reference. Phases are sequenced by leverage-over-risk, not by which is most
interesting.

- **Phase 1 — Tier parity + tool-call grader (the credibility floor).** Ship
  `local eval` and `cluster eval` (#344) and the **tool-call assertion** grader
  kind. Together these close the two failures the dogfood proved: evals that only
  run at one tier, and green-on-fake false confidence (#337, because a tool-call
  grader fails the fake model). Lowest new-contract surface — `tool_note` already
  carries the signal. This is the phase that makes the parity claim true.
- **Phase 2 — Trace → eval promotion (the headline capability).** `agentos eval
  add-from-trace` capturing input + trajectory + a curated assertion, with trace-id
  provenance (epic #26, #266). Depends on Phase 1's tool-call/trajectory capture so
  a promoted case can assert on behavior. This is the flow that turns production
  reality into a growing test suite.
- **Phase 3 — Trajectory + structured-result + gate assertions.** The approval-gate
  matcher (#262) and structured-result assertions, plus the guided interview on top
  of promotion (#260). This is where the taxonomy becomes complete; it carries the
  larger contract touch (tool results into the graded record).
- **Phase 4 — Variance and matrix.** Multi-sample / pass@k grading (#332), the
  pluggable scorer seam (#261), and the BYO-model matrix dimension (#255, feeding
  #313 and #326). These make eval results *trustworthy over noise* and *comparable
  across models* — most valuable once there is a rich trajectory grader to run *k*
  times and across models.

## Alternatives considered

- **Leave grading at final-text and tell authors to emit proof tokens.** Rejected:
  this is exactly the dogfood workaround, and it is a tax on every bundle author to
  reinvent a "was I called" signal the runtime already has on the wire. It also
  cannot express approval-gate or structured-result assertions at all, and it keeps
  the fake-model false-green (#337) unfixed.
- **A second, richer grader implementation in the CLI, independent of the platform.**
  Rejected: it reintroduces exactly the cross-language drift ADR-0017 and ADR-0019
  exist to prevent. One grader source of truth, generated or conformance-gated
  mirror — never two hand-authored graders that can disagree about pass/fail.
- **Add an `llm-judge` value to the frozen `GraderKind` enum now.** Rejected: an
  LLM-judge is non-deterministic and belongs above the frozen, reproducible grader
  port as a named scorer (#261), not inside the closed enum that eval-as-CI's
  reproducibility depends on. Adding it to `GraderKind` would erode the ADR-0019
  freeze's core property.
- **Auto-commit promoted trace cases.** Rejected: a captured trajectory is a
  candidate assertion, not a verified one. A production run can be a run someone
  *liked*, but "liked" is not "correct forever"; a human (or their coding agent)
  must curate the assertion before it becomes a gate. `add-from-trace` proposes and
  writes to the working tree; commit stays a reviewed act.
- **Build the BYO-model matrix (#255) first, since parity across models is
  compelling.** Rejected as a sequencing choice: a matrix over a text-only grader
  multiplies a weak signal across more models. The taxonomy and tier-parity work
  raise the signal quality that the matrix then amplifies, so the matrix lands last.

## Consequences

- **The parity claim becomes testable at all three tiers**, not just asserted. Once
  Phase 1 ships, "the same evals stay green skill → local → cluster" (ADR-0021) is a
  command a developer or coding agent can run, and a tier divergence is a real,
  reproducible environment-bug signal.
- **A green eval starts meaning "the agent did the work," not "a string appeared."**
  Tool-call and trajectory assertions make the fake-model path fail honestly (#337),
  which removes a class of false confidence rather than papering over it.
- **New contract surface is incurred deliberately and in order.** Server-side
  grading and structured-result assertions touch the frozen ACI/eval contract; each
  is an escalate-first change (ADR-0005/0017), scoped to the phase that needs it, not
  smuggled in as a side channel. Phase 1 is chosen specifically because it adds the
  least contract surface.
- **The eval suite becomes a living asset fed by production.** Trace → eval
  promotion means the highest-value cases accumulate from real runs, with provenance
  back to the trace (#266) — the compounding loop epic #26 is aimed at.
- **ADR-0019 stays intact.** The case *format* freeze holds: new grader kinds extend
  the enum through the same drift-gated schema mechanism, open-ended scorers plug in
  above the frozen port rather than inside it, and deny-by-default is preserved. This
  ADR extends ADR-0019; it does not reopen the freeze.
- **ADR-0004's deferred "version reproducibility lands with the eval lane" is now
  scoped here.** Trace promotion and the eval matrix both lean on Langfuse as the
  addressable trace + result store; this ADR is the lane ADR-0004 pointed at.
- **This ADR is a decision spine, not an implementation.** It fixes the model and the
  ordering; each phase's schema fields, combinator syntax, and command flags are
  settled in the issue and PR that builds it, against the constraints decided here.
