# 13. Concurrency and delivery: at-least-once streams with an idempotent, side-effect-aware kernel

Date: 2026-07-09
Status: Accepted

**Superseded in part by [ADR-0039](0039-bounded-delivery-and-a-dead-letter-graveyard.md)**
(back-link added under [ADR-0045](0045-the-status-line-is-the-mutable-part-of-an-immutable-adr.md)):
0039 bounds the unbounded `XAUTOCLAIM` reclaim clause of the Decision below with a
delivery cap and a dead-letter graveyard. Everything else in this ADR stands.

Retroactive record of the worker's concurrency model, already built into main.
The individual invariants each have an integration test under
`apps/worker/tests/`; this ADR records why the model is shaped this way and what
it forecloses.

## Context

The input side is unreliable in ways that matter for an agent that takes real
actions: Slack redelivers events, a worker can crash mid-turn, a user sends a
follow-up while a turn is still running, and a tool call may have already caused
a side effect (a posted message, an external write) before it failed. A naive
"read the queue, run the agent, retry on error" loop double-posts, double-acts,
and replays ancient backlog after a restart. The concurrency model is the
correctness core of the system.

## Decision

Transport is at-least-once and the kernel is made safe on top of it, rather than
chasing exactly-once in the broker. Concretely:

- **Transport: raw Valkey Streams with consumer groups** (`redis-py`,
  `XADD` / `XREADGROUP` / `XAUTOCLAIM`), one group for interactive runs
  (`curie:runs`) and a **separate** group for evals (`curie:evals`) so eval
  load never starves interactive turns
  ([`consumer.py:83`](../../apps/worker/src/curie_worker/consumer.py),
  [`eval/stream.py:216`](../../apps/worker/src/curie_worker/eval/stream.py)).
- **Dispatcher-side idempotency**: a `SET NX` on `dedupe:<event_id>` drops
  redelivered Slack events before they enqueue
  ([`apps/dispatcher/.../queue.py:73`](../../apps/dispatcher/src/curie_dispatcher/queue.py)).
- **One live session per thread**: a Valkey `SET NX PX` thread lock is the
  routing CAS ([`threadlock.py:60`](../../apps/worker/src/curie_worker/threadlock.py)).
- **The finish race**: a follow-up during a live turn is a steer; if the turn
  finished first the runner returns 409 and the kernel opens a fresh turn on the
  same idle sandbox ([`kernel.py:402`](../../apps/worker/src/curie_worker/kernel.py)).
- **No auto-retry after a side effect**: if a prior attempt flagged a side
  effect the kernel escalates to a human instead of retrying
  ([`kernel.py:201`](../../apps/worker/src/curie_worker/kernel.py)).
- **Crash recovery without backlog replay**: pending entries are reclaimed with
  `XAUTOCLAIM` ([`consumer.py:178`](../../apps/worker/src/curie_worker/consumer.py))
  and the runs group is created at `$` so a cold worker never replays old events.
- **`kernel.py` is sacred**: changes require escalated adversarial review, and
  the kernel never keyword-guesses intent.

## Alternatives considered

- **An exactly-once broker / message system.** Rejected: exactly-once delivery is
  expensive and still would not make a side-effectful tool call idempotent, so we
  would need the thread lock and the side-effect flag anyway. Putting the
  correctness at the side-effect layer is both cheaper and actually sufficient.
- **A job library such as BullMQ.** Considered (an early plan named it), and
  rejected in favour of raw Valkey Streams: we need direct control over
  consumer-group semantics, the create-at-`$` behaviour, and `XAUTOCLAIM` reclaim,
  and the extra dependency bought nothing the primitive did not already give us.
  The as-built queue is Valkey Streams via `redis-py`, not BullMQ.
- **Auto-retry every failed turn.** Rejected: it double-fires any tool that
  already caused a side effect. Escalation-instead-of-retry after a side-effect
  flag is the deliberate opposite choice.
- **A durable workflow engine (config-as-DAG) to get retries and state for
  free.** Rejected per ADR-0007: building a generic orchestration engine is
  commodity infra where we have no edge, and it would own the correctness we
  instead keep in a small, testable kernel.

## Consequences

- The kernel invariants are load-bearing behaviour: a "simplification" of the
  retry loop, the lock, or the finish race reintroduces duplicate side effects
  that pass unit tests and only surface under concurrent production load.
- Reclaim (ADR-0003's cold-rehydrate resume) rides on the same routing CAS, so
  the resume path is where duplicate side effects are most likely to hide.
- Shared stream-consumer helper extraction and promoting the turn payload into
  `aci-protocol` are follow-ups
  ([#9](https://github.com/curie-eng/curie/issues/9),
  [#7](https://github.com/curie-eng/curie/issues/7)); they must preserve these
  semantics, not just the surface API.
