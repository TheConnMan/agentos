# 27. A thin broker port at the non-sacred seams; defer the second broker

Date: 2026-07-13

Status: Accepted

Refines how ADR-0007 (adopt-not-build) applies to the queue/stream seam. Issue
[#284](https://github.com/curie-eng/agentos/issues/284), epic #85. Does **not**
supersede ADR-0007 — it keeps the "no second broker ahead of demand" rule and
changes only where the line is drawn in code, and is the queue-seam sibling of
ADR-0026 (storage port).

## Context

The queue/stream seam was SOFT: "the redis-py Stream verbs ARE the port," with
`redis.asyncio.Redis` / `redis.Redis` coupled at every call site (dispatcher
producer, worker consumer transport, worker crash-recovery reclaim). The stream
contract itself — one `payload` field, consumer groups, `XAUTOCLAIM` reclaim,
dedupe-beside-the-stream — is stable and known today; it is not a guess about a
future backend.

The hard constraint: the correctness kernel
(`kernel.py`/`consumer.py`/`threadlock.py`/`markers.py`) is **sacred** — one
change owns it at a time, adversarial review required, no scope-creep edits. The
`XREADGROUP`/`XACK`/`XAUTOCLAIM` verbs are split across a sacred file
(`consumer.py`, which calls `xautoclaim`) and non-sacred files (the
`StreamConsumer` transport base in `stream_consumer.py`; the dispatcher's
`queue.py` producer).

## Decision

Draw a thin **broker port** over the stream verbs **at the non-sacred seams
only**, and defer the second broker:

- **Consumer side** — `StreamBroker` `Protocol` (`apps/worker/.../broker.py`)
  declares the group/read/ack/reclaim verbs. `StreamConsumer` (non-sacred) is
  retyped to hold a `StreamBroker` instead of a concrete `Redis`. The verbs
  return a bare `Awaitable` (matching redis-py's typing) so
  `redis.asyncio.Redis` **structurally** satisfies the port with no adapter.
  The field stays named `_redis`, so the sacred `consumer.py` subclass — which
  reads `self._redis` for its `XAUTOCLAIM` reclaim — is **not edited**; its call
  now targets the port method by inheritance. `xautoclaim` is on the Protocol so
  the contract is complete even though only the sacred file calls it.
- **Producer side** — `StreamPublisher` `Protocol` (`apps/dispatcher/.../queue.py`)
  declares the two verbs the producer uses: `xadd` and the `SET NX EX`
  dedupe-claim. `enqueue`/`claim_event` are retyped to it. The producer is a
  *sync* client in a different package, so it is a separate role-Protocol; the
  two together are the broker seam.
- **No sacred file is touched, and no second broker is built.** Valkey Streams is
  the adopted spine (ADR-0007); this only makes the eventual swap (Redis Cluster,
  a managed stream, Kafka/NATS) a drop-in when a real second-broker demand
  arrives.

## Alternatives considered

- **Route the verbs through the port inside `consumer.py`.** Rejected: that is
  the sacred concurrency kernel; the sacred-module rule forbids editing it as a
  side effect of a seam refactor. The clean seam is the non-sacred transport
  base, which `consumer.py` inherits — so the port lands without touching it.
- **One unified broker Protocol for both sides.** Rejected: the producer is sync
  and the consumer is async, in two packages with no shared home; a single
  Protocol would be a false unification. Two role-Protocols is the honest shape.
- **Build a second (Kafka/managed) broker now.** Rejected: no second-broker
  demand, nothing to verify against — the speculative build ADR-0007 prevents.

## Consequences

- The stream contract is named and typed at both non-sacred seams; a second
  broker implements the same verbs and drops into the consumer transport and the
  producer with no call-site churn.
- **Remaining coupling:** `consumer.py`'s `XAUTOCLAIM` call and `run.py`'s client
  construction still reference redis directly (the sacred file by rule, the
  composition root by design). The API's `main.py` redis is the kill-switch /
  eval-queue, not the runs stream, and is out of scope. Fully unifying these is
  left for when the second broker lands. INTERFACE.md moves SOFT → CLEAN with
  this scope noted.
