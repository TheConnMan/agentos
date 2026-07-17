# 55. The fake model is a plumbing fixture, not a subject under test

Date: 2026-07-17

Status: Accepted

**Bounds [ADR-0022](0022-eval-completeness-tier-parity-and-trace-promotion.md)**
("Eval completeness"); it does not supersede it. ADR-0022's thesis — a green eval
means the agent did the work — holds unchanged for every tier that runs a real
model. This ADR says what the sentence means on the one tier where no model runs
at all. Per [ADR-0045](0045-the-status-line-is-the-mutable-part-of-an-immutable-adr.md),
0022 carries a status-line pointer here rather than an edited body.

Read [ADR-0041](0041-every-verb-is-answered-at-every-tier.md) for the exit-class
boundary this ADR leans on, and [ADR-0019](0019-freeze-eval-case-format.md) for
the frozen eval-**case** format this ADR does not touch.

## Context

The runner ships a fake model (`runner/src/agentos_runner/fake.py`) so the
authoring loop runs offline with no credential: `agentos skill up --fake-model`
is the fastest rung on the parity ladder, and it is the default in CI, in the
chart's sealed pool, and in compose (`${AGENTOS_FAKE_MODEL:-1}`). The fake is a
mock **at the adapter seam**: everything above it — translation, budget,
side-effect flagging, status, NDJSON, HTTP, the approval lifecycle — runs
unmodified. Its `default_turn()` returns one canned script whatever the input:
a text delta, a `Bash` tool note, and a final frame whose text is `all done`.

Two defects, filed separately, turned out to be one decision seen from two sides.

**[#612](https://github.com/curie-eng/agentos/issues/612): the documented
onboarding loop was red.** [#527](https://github.com/curie-eng/agentos/issues/527)
correctly removed the scaffold's vacuous `contains ""` grader (it passed on any
output, including an empty or errored turn) and
[#553](https://github.com/curie-eng/agentos/issues/553) replaced it with a
falsifiable `contains: <name>` — the agent must name itself. But the canned reply
`all done` does not contain the bundle name, so `init` -> `skill up --fake-model`
-> `skill eval`, the loop the product's own guide documents, failed on an
untouched scaffold. CI missed it because `cli/scripts/e2e.sh` wrote its own
`cases.json` with a grader tuned to match the fake's canned text.

**[#606](https://github.com/curie-eng/agentos/issues/606): fake runs were landing
in the matrix labelled with real model names.**
[#526](https://github.com/curie-eng/agentos/issues/526) gave `local eval` and
`cluster eval` a `--model` sweep so an operator could answer "can we move this
skill to a cheaper model", and `_eval_model` tagged each matrix cell with the
requested model. On a fake stack no model is ever called, so a sweep of N models
produced N rows of the same canned reply, each labelled with a real model id and
each carrying a pass-rate. The comparison an operator would make a promotion
decision on was fabricated.

The two obvious fixes are both wrong, and their wrongness is the actual finding:

- **Make the fake pass the grader** (echo the bundle name, or write graders that
  match `all done`). This is what #612's own note floats and what `e2e.sh`
  already did. It makes the fake pass a *content* grader — manufacturing exactly
  the false green #606 is about.
- **Make the scaffold's grader vacuous again** so the fake passes it. This
  re-introduces the unfalsifiable seed #527 removed, and an unfalsifiable case
  propagates into every bundle scaffolded from it.

Both fail because they answer "how do we make the fake tier green?" The question
is malformed. The fake never calls a model, so there is no agent behavior for a
content grader to have an opinion about. A green there was never evidence, and a
red there was never a defect.

## Decision

**The fake model is a plumbing fixture, not a subject under test.** Four rules
follow.

### 1. The fake tier asserts only that the turn completed. It is never graded.

A fake turn's outcome does not depend on the grader, because the grader is not
called. Implemented as an early return placed **after** the existing
`done`-status gate and **before** the scorer, in both languages:
`turn_outcome(case, events, fake)` (`cli/src/evals.rs`) and `_run_case`
(`apps/worker/.../eval/runner.py`). The ordering is load-bearing — see rule 2.

The grader itself stays **falsifiable** and unchanged. #527's intent is preserved
exactly: the seed still requires the agent to name itself, and still fails a
broken, empty, or off-topic turn on any real-model path. `plumbing_ok` is not a
backdoor to the vacuous grader; it is a statement that no grader ran.

### 2. A fake turn that does not complete is still a failure.

The fake tier's *one* assertion is that the turn completed. A non-`done` or
classified-failure fake turn means the plumbing genuinely broke, and that is the
one thing this tier must still catch. The `done` gate therefore runs before the
fake early return, not after it.

### 3. The outcome is `plumbing_ok`: a third state, not a shade of pass or fail.

`EvalOutcome` / `CaseOutcome` gain `plumbing_ok` alongside `pass` and `fail`. One
literal for the concept everywhere — the outcome value, the trace metadata, the
API's `EvalCell.status`, and the CLI's `--json` rollup key — so the vocabulary
cannot drift across the two languages.

`passed` survives as a **derived, tri-state** view: `pass -> true`,
`fail -> false`, `plumbing_ok -> null`. Derived, so no constructor can set
`outcome` and `passed` inconsistently — `outcome` is the single source of truth.
Tri-state rather than `false`, because `false` is a lie in the other direction:
it books a failure that did not happen, and a `0/3` rollup reads as a red run.
`null` drops out of both counts, and it is fail-safe — `None`/`null` is falsy, so
an unmigrated truthiness reader under-reports rather than false-greens. Both the
API's existing `_status` (which renders `passed=None` as `missing`) and
`_model_summaries` (which already skips `None`) therefore degrade honestly with
no migration.

Consequently: `failed` is **counted**, never inferred as `total - passed`, and an
all-plumbing run exits **0** — it is operationally successful without being a
pass. The rollup says so in words ("3 plumbing OK (not graded)"), never `3/3
passed`. The frozen `EvalReport` POST, whose `passed_count`/`total` shape cannot
express non-graded, is **skipped** for an all-plumbing fake run rather than made
to post a `0/N` false red or an `N/N` false green; a fake run containing a real
`fail` still posts, because that red is genuine.

### 4. Fake + `--model` is refused, Usage-shaped (exit 2), never Unsupported (4).

A sweep against a canned reply compares one string to itself, so the CLI refuses
it at the single `message::eval` seam both `local eval` and `cluster eval` funnel
through, with a message naming the reason and a fix naming the remedy
(`AGENTOS_CREDENTIALS` locally, `--set agentSandbox.runner.fakeModel=false` on
cluster).

**Exit 2, not 4, and the distinction is the whole of ADR-0041's boundary.** Exit
4 means the concept does not exist at this tier *by construction* — no input and
no retry changes it, and the only fix is another tier. That is not this. A model
sweep exists at this tier; supplying a credential makes this exact argv work. The
fix is an input, so it is a usage error. Exit 4 here would tell an agent to go
somewhere else when the answer was to bring a credential.

The default parity-gate run (no `--model`) on a fake stack is **not** refused: it
is the documented onboarding loop, and it claims nothing about any model.

### The worker is the sole authority on fake-ness

`AGENTOS_FAKE_MODEL` is set on the worker only, and the worker is the only
component that both knows fake-ness and sits upstream of grading. Two
consequences.

**Fake-ness never goes on the ACI wire.** The `Final` frame carries `text` and
`status` and no model field; `packages/aci-protocol` is a frozen contract, so a
new field costs a version bump plus tri-language codegen — to ship information
the worker already holds locally. The worker's `_eval_model` returning the
`fake-model` sentinel (never `item.model`) is the actual #606 invariant, and it
holds for every caller of the eval plane, not just the CLI.

**The API is not taught `AGENTOS_FAKE_MODEL`.** A second copy of the config would
drift from the truth, and it could not reproduce the chart's composite
(`fakeModel AND NOT inference.deploy`) anyway — an `inference.deploy` install
with `fakeModel=true` is a *real* install. The CLI's refusal is UX, not the
guarantee, and it reads the **deployed artifact's already-composed value**
(`docker inspect` on the compose worker; `kubectl` jsonpath on
`deployment/<release>-worker`) rather than re-deriving chart logic. Reading the
output honors the composite for free; re-deriving it would get it wrong. A probe
failure is reported as itself and never falls back to a guessed default, and
`--dry-run` does not probe at all — an offline plan must not claim what the
current runtime would do.

`POST /evals/trigger` remains callable directly, so the two guards are deliberate
defense-in-depth at different altitudes: different mechanisms, different layers.
If the CLI guard is bypassed, all N rows label `fake-model` and collapse to one
summary row — which is honest, because one thing ran.

## Consequences

**Every default `local eval --model` sweep now refuses**, because compose
defaults to `AGENTOS_FAKE_MODEL=1`. This is a visible behavior change for anyone
who was reading those numbers — but those numbers were fabricated, which is the
point. The refusal message carries the fix, so it should read as a remedy rather
than a regression.

**The scaffold's documented loop is green without being fake-green.** `init` ->
`skill up --fake-model` -> `skill eval` exits 0 and reports plumbing. The guide
and the scaffolded comments now say what the fake rung actually proves, rather
than calling it a promotion gate that "must be green".

**`e2e.sh` no longer writes graders tuned to the fake's canned text.** That bypass
is why CI missed #612; a suite written to match the fake asserts nothing.

**A mixed matrix keeps its graded signal.** `latest_by_model` keys on
`(version, case, model)` and the fake sentinel is a distinct key, so rows do not
collapse. In the displayed `(version, case)` cell, a **graded trace beats a
plumbing trace regardless of timestamp**: the grid is a promotion surface, a
plumbing row carries zero comparative information, and it must not erase graded
signal by being newer. Plumbing runs stay visible via the summary count.

**Costs accepted.** A third outcome state is a shape change rippling through two
languages, a committed JSON schema, and the matrix API. A tri-state `passed`
means a hypothetical strict external consumer could fail on `null` — bounded
here, since no in-tree consumer deserializes `passed` across a typed boundary,
and a loud failure on `null` is the ADR-0041-preferred outcome versus silently
ingesting a fabricated `false`.

**Not decided here.** The eval-case *content* ([#619](https://github.com/curie-eng/agentos/issues/619),
[#620](https://github.com/curie-eng/agentos/issues/620)), the tool-call grader
([#621](https://github.com/curie-eng/agentos/issues/621)), and unresolvable-model
validation ([#622](https://github.com/curie-eng/agentos/issues/622)). No
`GraderKind` is added: `plumbing_ok` is a *result* shape, not a *case* shape, so
ADR-0019's frozen case format is untouched.
