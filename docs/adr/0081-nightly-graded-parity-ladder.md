# 81. A nightly graded parity ladder verifies the real-model promise CI cannot

Date: 2026-07-24

Status: Accepted

Read [ADR-0055](0055-the-fake-model-is-a-plumbing-fixture.md) for the fake/graded
seam this ADR sits on the other side of, [ADR-0022](0022-eval-completeness-tier-parity-and-trace-promotion.md)
for what a graded green means, and [ADR-0068](0068-eval-sweep-fails-loudly-on-zero-completed-turns.md)
for the fail-loud-on-zero-completed-turns posture the graded rungs inherit.

Implements [#872](https://github.com/curie-eng/curie/issues/872).

## Context

The parity ladder (`curie dev e2e-ladder`) chains three rungs -- skill, local,
and cluster -- through the same cold-start round trip a user would take. In CI
(`ci.yaml`'s `e2e-ladder` and `e2e-ladder-cluster` jobs) every rung runs against
the fake model: the skill and local rungs default to it, and the cluster job
seals the install credential-free. ADR-0055 bounds exactly what those greens
mean: the fake model is a plumbing fixture, never a subject under test, so a
green fake rung proves the wiring carries a turn and nothing about whether a real
model, handed the weather bundle, actually answers.

That leaves the graded promise -- "a real model given the bundle produces a
correct answer end to end" -- verified only by a manual source run before a
release (`CURIE_E2E_TIERS=all curie dev e2e-ladder` with `CURIE_E2E_LIVE=1`). A
graded-path regression that the fake CI cannot see (a broken credential seam, a
closed egress route, a model-mode wiring change) would surface only at the next
release attempt, or in production. The gap is structural: the fake tier is
correct to stay ungraded, so the graded signal has to come from somewhere else.

## Decision

**Run the parity ladder graded on a nightly schedule, in addition to the fake
ladder on every PR.** A new scheduled workflow
(`.github/workflows/nightly-graded-ladder.yaml`) runs the skill, local, and
cluster rungs live against a real model over OpenRouter. It is the graded sibling
of the two fake ladder jobs, reusing their mechanics rather than inventing new
ones; the only job is to arm the model on the existing rungs.

**Cadence and time.** Nightly at 08:00 UTC (`cron: "0 8 * * *"`), an off-peak
slot. Nightly rather than per-PR because the graded run costs real model spend
and most PRs do not touch the graded seam; nightly rather than per-release
because a regression caught the morning after it lands is far cheaper to bisect
than one discovered weeks later at release time. Cron is always UTC, noted in the
workflow so the slot is not misread as local time.

**Cost.** Measured at roughly $0.053 per full ladder pass on the default model,
about $1.58 per month at nightly cadence (per #872). That price is why nightly is
affordable insurance; a graded regression reaching a release is worth far more
than a dollar a month of continuous verification.

**Model default.** `z-ai/glm-5.2` routed through OpenRouter, overridable via a
`workflow_dispatch` input for an ad hoc run against another route. An OpenRouter
key auto-selects the OpenRouter base URL, so no provider wiring beyond the
credential is needed.

**Key posture (#632).** The budget-capped key lives in the repo secret
`OPENROUTER_API_KEY` and reaches the ladder only as the `CURIE_CREDENTIALS` env
mapping the CLI reads -- never on an argv or `run:` line. On the cluster rung
`curie cluster up` consumes it from the environment, masks it, and adds the
fakeModel-off and credential values to the install itself; the secret never lands
in the Helm command line. The workflow declares least-privilege
`permissions: contents: read`, sets `persist-credentials: false` on every
checkout, pins every third-party action to the same commit SHA or tag the fake
ladder jobs use, and keeps the failure-diagnostics dump free of any command that
would print the credential value (the credential reaches the pod via secretKeyRef
and runner log redaction scrubs it).

**Trust boundary for the secret.** The workflow has no `pull_request` or
`pull_request_target` trigger, only `schedule` and `workflow_dispatch`, so a fork
PR has no path to the secret. `workflow_dispatch` requires repository write
access, so only collaborators can trigger a run; a collaborator dispatching
against a branch they control runs that branch's `cli/scripts/e2e-ladder.sh`,
so confidentiality ultimately rests on the trusted-collaborator set rather than
on branch protection. Residual risk is bounded: the key is budget-capped, and it
is passed only as a step-scoped masked `env:` value, never on argv or a `run:`
line. If the collaborator set grows, the available hardening is scoping the
secret to a protected GitHub Environment with a default-branch deployment
policy; that is out of scope here and the budget cap is the current control.

**Substrate is kind, not k3s.** The manual pre-release run happened on k3s, but
the CI-proven, gated cluster substrate is the kind job in `ci.yaml`. The nightly
cluster rung mirrors that kind job rather than standing up k3s from scratch,
because re-deriving a k3s install is out-of-scope risk against an already-working
substrate. This divergence from the manual run is intentional and recorded here.

**Fail loud, but only the skill rung is graded.** No `continue-on-error`
anywhere, and no path gate (a nightly always runs). The skill rung is the one
graded assertion in the ladder: it runs the weather eval cases and asserts a
genuine graded outcome (`passed:1 failed:0`, with `plumbing_ok:0` confirming
the grader itself ran rather than short-circuiting). The local and cluster
rungs do not grade content; they assert the live path end to end -- the turn
finalizes with a real reply through the full runner/worker plumbing -- and
under a live model add a negative control that rejects the fake model's
canned sentinel, proving a real model answered. Each rung still fails loud on
its own terms: a bad grade fails the skill rung, and an empty reply, a
non-finalized turn, or a sentinel reply fails the local or cluster rung. A
missing or rotated-away secret renders as an empty string that the live-mode
credential guard rejects with a hard exit, so a broken key fails loud rather
than silently downgrading to fake. Each job carries a `timeout-minutes` cap so
a hung turn cannot burn model budget up to the default six-hour ceiling. Job
topology is not fully independent: `ladder-cluster` is its own job and always
runs regardless of the other two, but `ladder-skill-local` runs the skill and
local rungs in one job via a single `e2e-ladder.sh` invocation with
`CURIE_E2E_TIERS: skill,local`, and the script runs tiers sequentially under
`set -e`, so a skill failure stops that job before the local rung runs (this
mirrors `ci.yaml`'s existing `e2e-ladder` job, which bundles the same two
tiers). The fail-loud guarantee is unchanged -- any rung failing reddens the
workflow -- but a skill failure can mask whether local would have passed.

## Consequences

- The graded promise is verified continuously, not once per release from memory.
  A graded-path regression surfaces the next morning with a bounded blame window.
- CI gains its first workflow that consumes a model-provider secret. The #632
  posture above is the whole of the added attack surface; it is reviewed against
  the entire action set the workflow pulls, not only the step that reads the key.
- The fake ladder in `ci.yaml` is unchanged and stays the fast per-PR gate. The
  two workflows are deliberately pinned to opposite sides of the ADR-0055 seam: a
  contract test fails if the nightly is ever de-armed or the fake ladder armed.
- A graded `local-release` rung (the sibling of `ci.yaml`'s `e2e-ladder-release`)
  is deliberately deferred: #872 does not ask for it and it needs extra release
  images built first. It can be filed as a follow-up if graded release-compose
  coverage is later wanted.
