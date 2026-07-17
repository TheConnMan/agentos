# 22. Eval completeness: run the same evals at every tier, grade what actually happened, and promote real traces into cases

Date: 2026-07-13
Status: Accepted

Gives the scattered eval work one decision spine. Extends
[ADR-0004](0004-langfuse-observability-and-eval-backbone.md) (Langfuse as the
observability + eval backbone) and [ADR-0019](0019-freeze-eval-case-format.md)
(the frozen eval-case format); it supersedes neither. It is the ordering and
decision reference for epic [#26](https://github.com/curie-eng/agentos/issues/26)
and the eval issues under it. Read
[ADR-0021](0021-agentos-is-a-harness-for-coding-agents.md) first: evals are the
contract the harness's parity promise is measured against.

[ADR-0055](0055-the-fake-model-is-a-plumbing-fixture.md) **bounds** this ADR
without superseding it: this ADR's thesis holds for every tier that runs a real
model, and 0054 says what it means on the fake tier, where no model is called and
so nothing is graded.

[ADR-0042](0042-llm-as-a-verifier-grader-and-progress-signal.md) extends this ADR
with the **semantic** grader (`GraderKind.verifier`) and supersedes one point of it:
this ADR's rejection of an LLM judge inside the frozen enum. The two divide by case
type — deterministic graders here assert what a trajectory makes checkable (a tool
was called, a gate fired); ADR-0042's verifier judges correctness where no string
match exists. ADR-0042 depends on the trajectory-forwarding decided here, so this
ADR's Phase 1 is its prerequisite. Everything else below stands.

## Context

The parity thesis (ADR-0021) is that **the same bundle plus the same
`evals/cases.json` stay green from `skill` to `local` to `cluster`**. Evals are the
contract that proves parity: a tier-to-tier divergence is the harness catching a
real environment bug, not the agent's skill logic.

**As a parity contract, the eval system does not yet prove what AgentOS advertises
it proves.** The execution surface is in place — the same suite does run at all
three tiers — but what a green eval *asserts* is still "a string appeared in the
final sentence," not "the agent did the work." A cold-start dogfood made this
concrete: to assert its deterministic engine had actually run, the agent had to
invent a tamper-proof "proof token" that the engine emitted and then grep the final
text for it, because no grader can check that a tool was ever called. Closing the
gap between what a green eval *appears* to prove and what it *actually* proves is
the purpose of this ADR.

Three axes are incomplete. Each is stated below as the code stands today.

**1. Grading sees only the final sentence.** The frozen grader taxonomy is
`exact | contains | regex`, and all three run against the turn's final answer text
(`apps/worker/src/agentos_worker/eval/models.py`, `GraderKind`). A grader can
confirm a version-shaped string appears in the reply; it cannot confirm the tool or
deterministic engine that was *supposed* to produce that string was ever **called**,
or that it returned a **structured result**. The trajectory is *already on the ACI
wire* — the runner emits a `tool_note` (carrying the tool name) and a
`side_effect_flag` for every tool use (`runner/src/agentos_runner/translate.py`) —
and the eval runner already captures those frames into a `trajectory` list
(`eval/runner.py`). Nothing in the frozen case format can assert against it. We are
grading a keyhole view of a run we can already see in full.

**2. A fake-model green is indistinguishable from a real one.** The fake model's
`default_turn` emits a scripted `ToolUseBlock(name="Bash")`
(`runner/src/agentos_runner/fake.py`), so on the fake-model CI path a text grader
greens without a model call — and a tool-call grader, once built, would green too.
The fake is a plumbing fixture, not a subject under test, but nothing in the eval
system says so. A green there proves the harness works, not the agent.

**3. Promotion captures the input but throws away the behavior.** `POST
/traces/{trace_id}/eval-case` (`apps/api/src/agentos_api/routers/runs.py`) reads a
Langfuse trace, extracts and anonymizes the conversation, and emits a case in the
frozen shape. But it grades with
`GraderOut(kind="contains", expected=_expected_snippet(anon_output))` — a `contains`
against a capped snippet of the output the run happened to produce
(`apps/api/src/agentos_api/evalcase.py`). It never looks at the trajectory, so a
promoted case asserts the agent says something similar, not that it does the same
work. There is no CLI verb, and provenance exists only as an `id` naming convention
(`promoted-<trace_id>`), not a field. The most valuable cases are the ones reality
writes; today promotion turns them into the weakest grader we have.

A fourth issue is not a harness gap but sets the standard the harness serves: **the
committed example suites are unfalsifiable.** `examples/weather/evals/cases.json`
passes on `contains: "weather"` for an input containing the word "weather"
([#527](https://github.com/curie-eng/agentos/issues/527)). Because `init` seeds new
bundles from these examples, a vacuous case is not one bad file; it is the pattern
every new agent inherits.

This ADR is not new machinery invented from scratch. ADR-0004 already chose Langfuse
as the trace + eval store and flagged per-run version reproducibility as "lands with
the eval lane" — this is that lane. ADR-0019 already froze the case *format* and
deliberately left the door open ("pluggable scorers via `GraderKind`, per-model
matrices") — this ADR walks through that door. What has been missing is a single
decision that says how the pieces fit and in what order they land.

## Decision

Evals are AgentOS's parity contract. We make that contract complete along three
axes — **it runs everywhere, it grades the whole run, and it is fed by reality** —
and we sequence the work so the highest-leverage, lowest-risk pieces land first.

### 1. Tier-parity execution: one suite, one grader, three rungs

The same `evals/cases.json` runs at `skill`, `local`, and `cluster` through **one
shared grader implementation**, invoked identically at every tier. A case grades
identically everywhere or the harness has a bug.

The three verbs exist (`cli/src/main.rs`), and the grading *discipline* is already
shared: the platform runner and the CLI both apply the `Done`-gate and
classified-failure gate before grading (`eval/runner.py`, mirrored by the CLI's
`turn_passes`), so a turn that ends idle or errors never grades green on matching
text. What is **not** settled is where the grader lives. Today it is
hand-mirrored — Python in the platform, Rust in the CLI — held together by
ADR-0019's byte-level conformance fixture. That is the **fallback** mechanism, and it
means every new grader kind is a second implementation to keep in lockstep.

The **preferred** end state is to grade server-side, at the runner: the runner
already owns the trajectory, so moving grading behind the `eval_case` ACI channel
makes every tier's CLI verb a dumb "submit case, print verdict" client and makes
cross-tier drift structurally impossible. It requires the `eval_case` result frame
to carry the graded outcome and the trajectory it graded — an ACI/frozen-contract
change that stops and escalates per ADR-0005/0017, not a side channel.

The choice between them is deferred to the phase that adds the first non-text
grader, because that is the phase that pays the mirroring cost. The **constraint** is
decided now: **the grader that decides pass/fail is defined in one place and the
tiers differ only in where the runner runs.** The grader is authored once, in
Python, as the source of truth ([ADR-0017](0017-tri-language-contract-codegen.md));
the CLI never grows an independently-authored grader.

### 2. Grader taxonomy: assert the trajectory, not just the last sentence

Extend `GraderKind` beyond text matchers. The taxonomy grows to cover what a real
eval needs to protect:

- **Text matchers** (`exact | contains | regex`) — unchanged, still on final text.
- **Tool-call assertion** — a named tool was invoked during the turn (e.g. the
  bundle's deterministic engine ran). This closes the dogfood proof-token
  workaround. The raw material exists: `tool_note` carries the tool name and
  `eval/runner.py` already accumulates the `trajectory` list; what is missing is a
  grader kind that can assert against it and a case format that can express it.
- **Structured-result assertion** — a tool returned a result matching a shape
  (present / non-empty / JSON-path predicate). This is what the dogfood's
  hand-rolled proof token was faking. It requires the tool *result* to reach the
  graded record — a larger contract touch than tool-call, since only the tool
  *name* is on the wire today — and is scoped to a later phase.
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

### 3. Fake-model runs must not produce an agent-quality green

The fake calls a scripted tool, so no grader kind can distinguish it from a real
model by inspecting the trajectory's *shape*. Since a richer taxonomy cannot fix
this, it is decided separately here: **a fake-model run must not be able to produce
a green that reads as an agent-quality signal.** The mechanism is chosen in the
phase that builds it; the two candidates are to refuse to record eval results from a
fake-model run at all (the fake is a plumbing fixture, and its green belongs to the
harness's own test suite, not to an agent's eval history), or an evidence check on
the trajectory's provenance rather than its shape (the family of
[#517](https://github.com/curie-eng/agentos/issues/517)). The first is cheaper and
is the default unless the phase finds a reason otherwise.

### 4. Trace to eval promotion: reality writes the best cases

Promotion must capture **behavior, not just words**. Extend the existing endpoint and
give it a CLI front door:

```
agentos eval add-from-trace <trace-id> [--assert tool:<name>] [--assert contains:<text>]
```

It reads the Langfuse trace (ADR-0004's store, addressed by trace id) and captures:

- **the input** — the user turn(s) that opened the run, verbatim, so the case
  replays the real prompt rather than a paraphrase. *(Built: `extract_io` + `redact`
  in `evalcase.py`.)*
- **the trajectory** — the ordered tool calls and any approval gates the run
  exercised, so the promoted case can assert on behavior. *(Not built: the endpoint
  reads observations but discards tool structure.)*
- **a graded assertion** — proposed from the trajectory (e.g. "tool `X` was called
  and the final text contains `Y`"), which the human **curates** before commit.
  *(Not built: today it emits a `contains` on a capped output snippet, which is the
  weak-grader default this ADR exists to move off.)* The command writes a case into
  `evals/cases.json` in the frozen ADR-0019 shape; it does not silently commit — the
  developer (or their coding agent, per ADR-0021) reviews and edits the assertion,
  because a captured trajectory is a *candidate*, not a truth.

Provenance is recorded as a **field** on the promoted case, not inferred from the
`id` string: the originating trace id is stamped so a case can be traced back to the
run it came from. A guided, interview-style variant of this flow is
[#260](https://github.com/curie-eng/agentos/issues/260) and layers on top of the same
capture pipeline; per ADR-0021 that interview is agent-conducted, and the CLI stays
the non-interactive `add-from-trace` mechanism underneath. This promotion pipeline is
the spine of epic #26.

### 5. Multi-sample / variance-aware grading

Real model runs are non-deterministic; a single sample is a coin flip reported as a
fact. Grading gains an optional sample count and a pass rule
([#332](https://github.com/curie-eng/agentos/issues/332)): run a case *k* times and
pass on a threshold (majority, or pass@k / all-of-k for a flake-intolerant gate).
The default stays **1** so today's suites and the fake-model CI path are unchanged
and cheap; multi-sample is opt-in per case or per run, because it multiplies cost and
latency. `EvalRunResult` already records per-case rows; variance grading extends it
to carry the sample distribution, not just a boolean. This axis is scheduled after
the taxonomy, since a variance gate over a text-only grader is far less useful than
one over a trajectory grader.

### 6. Pluggable scorer seam and BYO-model matrix

- **Pluggable scorer seam**: the grader taxonomy above is deliberately a *closed,
  deterministic* enum, because reproducibility is the point of eval-as-CI (ADR-0004,
  ADR-0019). The extension point for open-ended scorers (an LLM judge, a custom
  Python scorer) is a **named seam above the frozen grader port**, not a new
  `GraderKind` value. A scorer plugs in by name; the frozen case schema stays closed
  and reproducible. The seam itself is built (`eval/scorer.py`: the `Scorer`
  protocol, `GraderScorer` as the default, and the `scorer=` parameter on
  `EvalRunner`), and a `TrajectoryScorer` with five match modes is built and
  unit-tested — but **nothing injects it**: no CLI flag and no Job config supplies
  the `case_id -> TrajectorySpec` map it needs, so it cannot be run end to end
  ([#389](https://github.com/curie-eng/agentos/issues/389)).

  **Semantic grading is out of scope here and governed by
  [ADR-0042](0042-llm-as-a-verifier-grader-and-progress-signal.md).** This ADR
  originally rejected an LLM judge inside the frozen enum on reproducibility
  grounds; ADR-0042 supersedes that specific rejection with a `GraderKind.verifier`
  whose continuous scoring and `K` repeated evaluations answer the objection on the
  evidence (Kwok et al., arXiv:2607.05391). The division of labor is by **case
  type, not by preference**: deterministic graders assert the mechanical facts a
  trajectory makes checkable (a tool was called, a gate fired, a result has a
  shape) — cheap, zero-variance, and what the parity contract rests on; the
  verifier grades semantic correctness where no string match exists. An LLM judge
  is the wrong instrument for "was this tool called," which is a set-membership
  check over structured wire data. Note that neither addresses run-to-run variance,
  which is the model's non-determinism and is answered only by multi-sample (axis
  5, #332).
- **BYO-model eval matrix**: the run-result types are already keyed by `version`; the
  matrix is the natural place for a **model** dimension so the same suite grades
  across models and results compare side by side. The dimension is built
  (`EvalRunResult.model` -> a Langfuse `model:` tag -> `GET /evals/matrix`), but two
  pieces are missing: `EvalCaseResult.cost_usd` is declared and read by the matrix
  yet **never populated**, so every per-model cost rollup reads `None`
  ([#390](https://github.com/curie-eng/agentos/issues/390)); and no eval verb accepts
  a model, so there is no way to select one at eval time or sweep a suite across
  several ([#526](https://github.com/curie-eng/agentos/issues/526)). This axis is a
  reporting/fan-out concern that composes cleanly once the taxonomy is richer, and it
  feeds the cross-harness parity evals
  ([#313](https://github.com/curie-eng/agentos/issues/313)) and the harness eval
  delta ([#326](https://github.com/curie-eng/agentos/issues/326)), both matrix
  consumers rather than new grading machinery.

## What is built, and what this ADR asks for

Verified against the tree at acceptance. "Inert" means the code exists and is
unit-tested but no runnable path reaches it.

| Capability | State | Issue |
|---|---|---|
| `skill` / `local` / `cluster eval` verbs | **built** | #344 |
| Shared `Done`-gate + classified-failure gate across tiers | **built** | — |
| Grader source of truth (server-side vs hand-mirrored) | **undecided** — hand-mirrored today | — |
| Text graders (`exact`/`contains`/`regex`) on final text | **built** | ADR-0019 |
| Trajectory captured by the eval runner | **built** (captured, ungradeable) | — |
| Tool-call assertion grader kind | **not built** | — |
| Structured-result assertion grader kind | **not built** | — |
| Approval-gate / trajectory matcher grader kind | **not built** | #262 |
| Fake-model run cannot yield an agent-quality green | **not built** | #517 |
| Scorer seam above the frozen port | **built** | #261 |
| `TrajectoryScorer` (5 modes) | **built but inert** | #389 |
| Semantic grader (`GraderKind.verifier`) | **not built** — see ADR-0042 | #478 |
| Trace promotion: input capture + anonymization | **built** | #259 |
| Trace promotion: trajectory capture + curated assertion | **not built** | #26 |
| Trace promotion: trace-id provenance as a field | **not built** (id convention only) | #26 |
| Trace promotion: CLI `eval add-from-trace` | **not built** | #26 |
| Guided eval-generation interview (`skill eval-init`) | **built** | #260 |
| Multi-sample / pass@k grading | **not built** | #332 |
| Matrix model dimension | **built** | #255 |
| Matrix per-model cost | **built but dead** (no producer) | #390 |
| Model selection / sweep at eval time | **not built** | #526 |
| Falsifiable committed example suites | **not built** | #527 |

## Phased plan

Phases are sequenced by leverage-over-risk, not by which is most interesting.

- **Phase 1 — The credibility floor.** The **tool-call assertion** grader kind, and
  the fake-model decision from axis 3. Together these make a green mean "the agent
  did the work": the grader closes the dogfood proof-token workaround, and the
  fake-model rule stops the CI path from minting greens that read as agent quality.
  Lowest new-contract surface — `tool_note` already carries the signal and the runner
  already captures it. Tier parity (#344) is already done, so this is what remains of
  the floor. Settle the grader-source question (axis 1) here, since this is the first
  grader kind that would otherwise be hand-mirrored twice.
- **Phase 2 — Trace to eval promotion (the headline capability).** `agentos eval
  add-from-trace` capturing input + trajectory + a curated assertion, with trace-id
  provenance as a field, extending the existing endpoint rather than replacing it
  (epic #26). Depends on Phase 1's tool-call grader so a promoted case can assert on
  behavior instead of defaulting to a `contains` snippet. This is the flow that turns
  production reality into a growing test suite.
- **Phase 3 — The rest of the taxonomy.** The approval-gate matcher (#262) and
  structured-result assertions, plus the guided interview layered on promotion
  (#260, whose starter-suite generator already exists). This carries the larger
  contract touch: tool *results* must reach the graded record, where only tool names
  are on the wire today.
- **Phase 4 — Variance, reach, and matrix.** Multi-sample / pass@k grading (#332);
  wiring the built-but-inert `TrajectoryScorer` into a runnable path (#389); a
  producer for the cost axis (#390); and eval-time model selection / sweep (#526),
  feeding #313 and #326. These make eval results *trustworthy over noise* and
  *comparable across models* — most valuable once there is a rich trajectory grader
  to run *k* times and across models.
- **Cross-cutting — the cases themselves (#527).** Every phase above improves the
  *harness*; none improves the *suites* it runs. Rewrite the committed examples so
  each case is falsifiable, tracking alongside Phase 1 since the tool-call grader is
  what several of them need. The standard for a case is the same one this ADR sets
  for a grader: **could a plausibly-broken agent pass it?** If yes, it is not an eval.

## Alternatives considered

- **Leave grading at final-text and tell authors to emit proof tokens.** Rejected:
  this is exactly the dogfood workaround, and it is a tax on every bundle author to
  reinvent a "was I called" signal the runtime already has on the wire. It also
  cannot express approval-gate or structured-result assertions at all.
- **A second, richer grader implementation in the CLI, independent of the platform.**
  Rejected: it reintroduces exactly the cross-language drift ADR-0017 and ADR-0019
  exist to prevent. One grader source of truth, generated or conformance-gated
  mirror — never two hand-authored graders that can disagree about pass/fail.
- **Add an `llm-judge` value to the frozen `GraderKind` enum now.** Originally
  rejected on the grounds that an LLM judge is non-deterministic and belongs above
  the frozen grader port as a named scorer, not inside the closed enum that
  eval-as-CI's reproducibility depends on. **Superseded by ADR-0042 on this point.**
  That rejection treated the judge's non-determinism as disqualifying; ADR-0042
  shows it is a knob rather than a property — a continuous reward computed as the
  expectation over the scoring-token distribution, averaged over `K` repeated
  evaluations (variance `O(1/K)`) and `C` criteria, is reproducible enough to gate
  on. The rest of this ADR is unaffected: ADR-0042 extends it and depends on the
  trajectory-forwarding decided in axis 2.
- **Fix fake-model false-green with a tool-call grader.** Rejected on the facts: the
  fake emits a scripted `ToolUseBlock`, so it greens a tool-call grader exactly as it
  greens a text grader. No grader kind can separate a scripted trajectory from a real
  one by shape, which is why axis 3 decides it separately.
- **Auto-commit promoted trace cases.** Rejected: a captured trajectory is a
  candidate assertion, not a verified one. A production run can be a run someone
  *liked*, but "liked" is not "correct forever"; a human (or their coding agent) must
  curate the assertion before it becomes a gate. `add-from-trace` proposes and writes
  to the working tree; commit stays a reviewed act.
- **Build the BYO-model matrix first, since parity across models is compelling.**
  Rejected as a sequencing choice: a matrix over a text-only grader multiplies a weak
  signal across more models. The taxonomy work raises the signal quality that the
  matrix then amplifies, so the matrix lands last.

## Consequences

- **A green eval starts meaning "the agent did the work," not "a string appeared."**
  Tool-call and trajectory assertions grade the run rather than its last sentence,
  which removes a class of false confidence rather than papering over it.
- **The fake-model CI path stops minting agent-quality greens.** Axis 3 makes the
  fake honest about what it is: a plumbing fixture whose green belongs to the
  harness's test suite, not to an agent's eval history.
- **New contract surface is incurred deliberately and in order.** Server-side
  grading and structured-result assertions touch the frozen ACI/eval contract; each
  is an escalate-first change (ADR-0005/0017), scoped to the phase that needs it, not
  smuggled in as a side channel. Phase 1 is chosen specifically because it adds the
  least contract surface.
- **The eval suite becomes a living asset fed by production.** Trace to eval
  promotion means the highest-value cases accumulate from real runs, with provenance
  back to the trace — the compounding loop epic #26 is aimed at. It also upgrades
  promotion from its current weak default rather than leaving it as a `contains`
  snippet generator.
- **Built-but-unreachable code is treated as unbuilt.** The scorer seam's
  `TrajectoryScorer` (#389) and the matrix's cost axis (#390) both shipped structurally
  complete and functionally inert. This ADR counts a capability as done when a
  runnable path reaches it, which is why both sit in Phase 4 rather than in the
  built column.
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
