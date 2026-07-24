# 18. Greeting/help pre-model short-circuit in the kernel

Date: 2026-07-09
Status: Accepted

Status corrected from `Proposed` under
[ADR-0045](0045-the-status-line-is-the-mutable-part-of-an-immutable-adr.md): the
short-circuit is wired. The Context below records that the matchers had "**zero
live callers**"; they are now called from the kernel itself
(`apps/worker/src/curie_worker/kernel.py::Kernel._route_and_start`, which evaluates
`match_greeting(packs, event.text) or match_help(packs, event.text)` under the
route lock). The Context is left as written — it is the record of what was true
when the decision was taken.

## Context

The behavior-packs substrate (ADR-adjacent work, CUR-1586) shipped opt-in,
per-agent `greeting` and `help` packs: declarative phrase lists plus a canned
reply. The pure matchers `match_greeting` / `match_help`
(`apps/worker/src/curie_worker/behaviorpacks.py`) are implemented and
unit-tested but have **zero live callers** — the packs are configurable today but
never fire. The intended payoff is a strict cost win: a bare "hi" or "help" is
answered from the already-posted placeholder without claiming a sandbox or
calling the model.

Wiring this is not a normal feature: the only place it can fire is inside
`Kernel.process_event` / `_attempt`, the F1 "sacred" concurrency kernel, which
`apps/worker/CLAUDE.md` gates behind an adversarial review and single-owner rule.
Two kernel invariants make the naive placement wrong:

- **Rule 1 (one live session per thread).** A follow-up to a thread with a live
  turn is a *steer*, never a new turn. `docs/behavior-packs.md` sketched the
  short-circuit in `process_event` *before the retry loop* — but the
  steer-vs-new-turn decision is only made later, inside `_attempt` under the
  per-thread lock (`_route_and_start`). At that earlier point the kernel does not
  yet know whether the event is a new turn or a steer, so matching "hi" there
  would **drop a steer** to a live turn. The doc asserts a guarantee ("runs only
  for a new turn") that its own placement does not provide.
- **Rule 3 (the kernel never keyword-guesses intent).** Rule 3 forbids
  text-sniffing to decide *steer vs. interrupt*. This short-circuit is a
  different decision (answer a **new** turn canned vs. invoke the model), but it
  still introduces text matching into the kernel, so it must be a deliberate,
  narrowly-scoped exception rather than an ad-hoc keyword check.

## Decision

Wire the greeting/help short-circuit **inside the route critical section**, gated
on the thread being provably fresh, so rule 1 holds by construction.

- In `_route_and_start` (already running under the per-thread Valkey lock), BEFORE
  claiming a sandbox: if the agent has an enabled greeting/help pack whose matcher
  matches the message text **and** `substrate.lookup(thread) is None` (the thread
  has no existing sandbox route), return a short-circuit result carrying the
  canned reply — without claiming a sandbox or calling `start_turn`.
- `_attempt` delivers the canned reply onto the existing placeholder and returns a
  terminal-ok outcome; `process_event` marks the event done. No run is registered,
  no sandbox claimed, no model turn started.
- **`lookup(thread) is None` is the rule-1 guarantee.** A thread with *any* live
  route (or a since-idle but still-claimed sandbox) is never short-circuited — it
  falls through to the normal claim → steer/`start_turn` path. Because the check
  and the routing both happen under the same lock, no live turn can appear between
  the check and the decision. A steer therefore can never be swallowed by the
  short-circuit.
- The matchers are **opt-in, per-agent, declarative, deterministic, and
  pre-model.** This is the scoped exception to rule 3's spirit: it is not the
  kernel guessing steer-vs-interrupt intent from text; it is a configured,
  data-driven reply the agent owner explicitly enabled.

## Consequences / trade-offs

- **Conservative on returning threads.** A greeting on a thread whose sandbox is
  still claimed but idle (between turns) is *not* short-circuited — it routes
  normally and hits the model. We trade that cost-optimization for rule-1
  safety-by-construction. The dominant real case (a fresh "hi" opening a thread)
  is fully optimized: no sandbox, no model.
- **Saves both sandbox and model** on the fresh-thread path (better than the doc's
  sketch, which — placed correctly — could at best skip the model after a claim).
- Kernel diff stays small and is plumbing plus one guarded early return; it is
  covered by a provoking integration test for the steer case (a mid-live-thread
  "hi" must STEER, not be dropped) as `apps/worker/CLAUDE.md` requires, plus the
  adversarial review.

## Alternatives considered and rejected

- **Doc's placement (in `process_event` before `_attempt`).** Simple, but not
  rule-1-safe: it fires before steer-vs-new is decided and would drop steers.
- **Short-circuit after `route.steered` is known, before `start_turn`.** Rule-1
  safe, but the sandbox is already claimed by then, so it only saves the model
  call, not the pod. The lock-guarded `lookup is None` check saves both.
- **A cheap pre-lock `lookup` in `process_event`.** Loses the lock's atomicity —
  a live turn could start between the lookup and the decision (TOCTOU), so a steer
  could still be dropped. Rejected in favor of the in-lock check.
