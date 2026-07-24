# 51. Eval cases run in a fresh conversation by default, with per-case opt-in shared history

Date: 2026-07-17

Status: Accepted

Implements [#550](https://github.com/curie-eng/curie/issues/550). Extends
ADR-0019 (the frozen eval-case format): it adds one optional field to that schema
and pins the runtime isolation semantics the format now implies. It does not
supersede ADR-0019; the suite-object-with-graders shape stays canonical and
frozen in place.

## Context

Both eval-suite runners drive their cases sequentially against **one** runner
endpoint: the platform worker (`apps/worker/.../eval/runner.py`) and the CLI's
`curie skill eval` (`cli/src/commands.rs::run_suite_cases`). The runner holds
one long-lived model session per process (ADR-0003, for prompt-cache affinity
across a thread's turns), so every case in a suite ran **inside the same growing
conversation**. Case N could see cases 1..N-1's messages.

A cold-start end-to-end run on v0.4.0-rc.4 surfaced the failure directly:

> Case 3 answered from case 2's history without re-calling the tool. Fine for
> prompts, quietly wrong for side-effecting agents.

For an agent whose whole job is a side effect (close an issue, post a message,
call an API), a case that answers from conversational memory instead of actually
invoking the tool is a **false green**: the eval reports pass while the behavior
under test never ran. It is the same failure class as a test asserting on a mock
instead of the thing under test, which this repo already treats as a real bug
(`fake.py`, the never-mock-the-thing-under-test rule). It also makes case
**ordering** silently load-bearing: the suite's verdict depends on which case ran
first, invisibly.

Note the two other eval paths that already isolate and are therefore out of
scope. The `local message` / `cluster message` eval path
(`cli/src/message.rs::run_eval_turns`) gives each case its own thread key, so the
kernel routes each to its own session — no cross-talk. And the platform's
Job-orchestration layer fans out a fresh **sandbox** per version @ sha. This ADR
is about the per-case guarantee an in-process suite runner gives against **one**
endpoint, which neither of those covers.

## Decision

**Each eval case runs in a fresh conversation by default. Shared history is an
explicit, per-case opt-in.**

### 1. A runner control route resets the conversation

The runner exposes `POST /v1/reset`: it tears down the current SDK session and
reconnects a new one from the same factory, so the next turn starts with no
accumulated conversation. It is a runner control route (like `/status` and
`/healthz`), **not** an ACI wire frame — so it needs no `aci-protocol` version
bump and does not touch the frozen wire contract. It refuses with 409 while a
turn is active, mirroring the steer finish-race boundary, and is gated by the
same bearer token as the other `POST` routes.

This is a deliberate, explicit control — **not** per-turn session churn. The
one-long-lived-session-per-process invariant still holds for the message path,
which never calls reset. A thread with a durable `CURIE_HISTORY_REF` still
rehydrates *its own* history preamble on reconnect (that is the thread's real
history, not a cross-case leak); an eval runner, which has no history ref, comes
up empty.

### 2. The suite runners reset before each fresh-conversation case

Both suite runners issue a reset before a case unless that case opts out. Reset
runs before **every** non-shared case, including the first, so a case is truly
fresh even against a persistent `skill up` runner that carries prior
`skill message` history — "fresh by default" does not depend on the endpoint
having been freshly booted.

If isolation cannot be established (the reset call errors), the worker **fails
that case** with the error rather than running it against a possibly-leaked
conversation — a false green is exactly what this ADR removes. One failed reset
never aborts the suite, matching the existing per-case error handling.

### 3. `shared_history` is the per-case opt-out

`EvalCase` gains an optional `shared_history: bool` field, default `false`. A
case that sets it `true` skips the reset and deliberately inherits the prior
case's conversation — a multi-turn scenario expressed as ordered cases. It is a
new **optional** field with a default, so it is a backward-compatible addition to
the frozen schema (ADR-0019): an existing suite that omits it keeps decoding, the
CLI mirror omits a `false` value on serialize so authored `cases.json` files stay
byte-identical, and the drift gate
(`tests/eval/test_schema_compat.py`, `scripts/check-contracts.sh`) is
regenerated in the same change per ADR-0019's mechanism.

## Alternatives considered

- **A fresh sandbox per case.** The strongest isolation, and it is already the
  Job layer's choice for cross-version fan-out. Rejected as the per-case default:
  a container per case is far slower and heavier than an in-session reset, and
  the AC asks for a fresh *conversation*, not a fresh *pod*. Sandbox isolation
  stays available at the layer that already owns it.
- **A `new_conversation` flag on the ACI `Event` frame.** Rejected: it is a
  change to the frozen wire contract (a new field on a frozen model, a version
  bump, cross-language regeneration) for what is really a session-control action,
  not message content. A control route keeps the wire contract untouched.
- **Reset only between cases, never before the first.** Rejected: a persistent
  `skill up` runner can carry history from a prior `skill message`, so the first
  case would silently inherit it. Resetting before every non-shared case makes
  the guarantee uniform and endpoint-state-independent.
- **Make isolation the only behavior, no opt-in.** Rejected: the AC requires
  that shared history be expressible when genuinely wanted (a multi-turn
  scenario). A per-case flag keeps the default safe while leaving the door open,
  explicitly.
- **Swallow a failed reset and run the case anyway.** Rejected: it reintroduces
  the exact false green — a case run against un-cleared history reports a pass the
  behavior never earned. A reset that cannot be established fails the case.

## Consequences

- **Behavior change suite authors must know about:** a suite that quietly relied
  on an earlier case priming a later one now goes red on the later case unless it
  is marked `shared_history: true`. That red is the point — it exposes a false
  green and a load-bearing ordering that were previously invisible.
- **Eval runs are slightly slower.** A fresh-conversation case pays one session
  reconnect (a fresh SDK session, no prompt-cache reuse across cases). This is
  the intended trade: per-case correctness over cross-case cache affinity, which
  was never a property evals should have depended on.
- **The regression is pinned.** A worker test drives a stateful fake runner where
  a recall case can only pass by inheriting a prior case's history; it now fails
  under the default and passes only when the case opts into `shared_history`, and
  a failed reset fails the case rather than leaking history.
- **The wire contract is untouched.** `/v1/reset` is a runner control route, so
  no `aci-protocol` version bump and no conformance change; only the eval-case
  schema (a derivative of the worker Pydantic models, ADR-0019) moves, additively.
