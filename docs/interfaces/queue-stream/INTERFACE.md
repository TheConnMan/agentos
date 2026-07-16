---
seam: Queue / stream (Valkey)
kind: CLEAN
impls: 1 (redis-py) behind the broker port
grade: not separately graded
epics:
  - "#85"
  - "#7"
order: 11
---
# INTERFACE: Queue / stream (Valkey)

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).
<!-- BEGIN GENERATED: header (agentos dev docs-lint) -->
> **Kind:** CLEAN &nbsp;·&nbsp; **Implementations today:** 1 (redis-py) behind the broker port &nbsp;·&nbsp; **Swap-readiness grade:** not separately graded
<!-- END GENERATED: header -->

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

The seam is the Valkey Stream wire contract between the dispatcher (producer) and the
worker (consumer): a named stream carrying a frozen one-field payload. As of #284 /
ADR-0027 the stream verbs are drawn behind a thin **broker port** at the two non-sacred
seams — a `StreamPublisher` `Protocol` on the producer (`apps/dispatcher/src/agentos_dispatcher/queue.py::StreamPublisher`:
`xadd` + the `SET NX EX` dedupe-claim) and a `StreamBroker` `Protocol` on the consumer
transport (`apps/worker/src/agentos_worker/broker.py::StreamBroker`: `xgroup_create`/`xreadgroup`/`xack`/`xautoclaim`).
The routing, consumer-group concurrency, dedupe, and reclaim rules stay opinionated
**core**. `redis.Redis` / `redis.asyncio.Redis` structurally satisfy the ports, so
redis-py is the one backing today with no adapter; a redis-compatible backend (Valkey,
Redis, a managed equivalent) is still a URL change, and a non-redis broker (Kafka, SQS)
is now a drop-in implementation of the two Protocols rather than a grep-and-replace of
every call site. The **second broker itself is not built** — no second-broker demand
exists (ADR-0007); only the port is extracted.

## Current contract

A second broker must honor the stream key, the payload encoding, and the Stream verbs:

- **Stream key** — `"agentos:runs"`, defaulted identically on both ends:
  `DispatcherConfig.stream` (`apps/dispatcher/src/agentos_dispatcher/config.py::DispatcherConfig`,
  env `AGENTOS_STREAM`) and `WorkerConfig.stream`
  (`apps/worker/src/agentos_worker/config.py::WorkerConfig`).
- **Payload encoding** — one Stream field, `STREAM_PAYLOAD_FIELD = "payload"`
  (`apps/dispatcher/src/agentos_dispatcher/queue.py::STREAM_PAYLOAD_FIELD`),
  holding `model_dump_json()`. Produced by `enqueue` via `redis_client.xadd(config.stream, fields)`
  (`apps/dispatcher/src/agentos_dispatcher/queue.py::enqueue`) and reconstructed by
  `from_stream_fields` (`apps/dispatcher/src/agentos_dispatcher/queue.py::from_stream_fields`)
  into a `QueuedTurn`.
- **Consumer verbs** — the worker reads with `xreadgroup` over a consumer group
  (`apps/worker/src/agentos_worker/consumer.py::Consumer._read_loop`), rebuilds the model at
  `apps/worker/src/agentos_worker/consumer.py::Consumer._handle`, and acknowledges with `xack`
  (`apps/worker/src/agentos_worker/consumer.py::Consumer._ack`). The group is
  `"agentos-workers"` (`WorkerConfig.consumer_group`, `apps/worker/src/agentos_worker/config.py::WorkerConfig`).

Idempotency lives beside the stream, not in it: `claim_event` does a
`SET <dedupe_key> 1 NX EX <ttl>` before `XADD` (`apps/dispatcher/src/agentos_dispatcher/queue.py::claim_event`).

## Implementations today

One, redis-py against Valkey. The dispatcher `XADD`s
(`apps/dispatcher/src/agentos_dispatcher/queue.py::enqueue`); the worker runs
a consumer group with `XREADGROUP`/`XACK` and crash-recovery `XAUTOCLAIM`
(`apps/worker/src/agentos_worker/consumer.py`). A second, sibling stream
`"agentos:evals"` uses the same one-field `payload` convention
(`apps/worker/src/agentos_worker/eval/stream.py`), reinforcing the wire shape as the
real contract.

## The port (as of #284 / ADR-0027)

Drawn only at the **non-sacred** seams; the sacred concurrency kernel
(`apps/worker/src/agentos_worker/kernel.py` / `apps/worker/src/agentos_worker/consumer.py` /
`apps/worker/src/agentos_worker/threadlock.py` / `apps/worker/src/agentos_worker/markers.py`)
is not touched:

- **Producer** — `StreamPublisher` (`apps/dispatcher/src/agentos_dispatcher/queue.py::StreamPublisher`): `xadd` and the
  `SET NX EX` dedupe-claim. `enqueue`/`claim_event` type against it.
- **Consumer transport** — `StreamBroker` (`apps/worker/src/agentos_worker/broker.py::StreamBroker`):
  `xgroup_create`/`xreadgroup`/`xack`/`xautoclaim`. The non-sacred `StreamConsumer`
  base (`apps/worker/src/agentos_worker/stream_consumer.py`) holds a `StreamBroker`; the sacred `consumer.py` subclass
  inherits it unchanged (its `XAUTOCLAIM` reclaim now targets the port by inheritance).

The verbs return a bare `Awaitable`/value matching redis-py's own typing, so
`redis.asyncio.Redis` / `redis.Redis` satisfy the ports structurally with no adapter.

## Known leakage

- **Reclaim + composition root still touch redis directly.** `consumer.py`'s
  `XAUTOCLAIM` call (sacred file, by rule) and the client construction in
  `apps/worker/src/agentos_worker/run.py` (the composition root, by design) reference
  redis directly.
- **The API writes the runs stream outside the broker port.** Correcting an earlier claim
  that the API's redis only backs the kill-switch / eval-queue: the approval-resume path
  enqueues resume turns directly onto the same `agentos:runs` stream via `ResumeQueue`
  (`apps/api/src/agentos_api/resumequeue.py::ResumeQueue`) and the expiry sweeper, an `xadd`
  that bypasses the dispatcher's `StreamPublisher` port entirely. A second broker must
  account for this third producer (the API) that does not go through the port today.
- **The redis-py exception surface leaks.** The ports type the verbs but not the error
  contract: `redis.exceptions` propagate through the callers unabstracted, so a non-redis
  broker must either raise redis-py-compatible exceptions or the call sites must learn its
  error types.
- **Payload vendor-neutrality (now closed on this seam).** The payload was once Slack-shaped
  by name; #7 promoted it into `packages/aci-protocol` as the channel-neutral
  `QueuedTurn`, so the queue seam no longer carries Slack-shaped field names. The queue
  seam's own remaining constraint is narrower: the contract assumes redis Stream semantics
  (ordered entries, consumer groups, pending-entry reclaim), so a swap that stays
  redis-compatible is a URL change while a non-redis broker rewrites the wire and the
  consumer verbs.

## Cross-links

- **Epic(s):** #85 — vision: make the broker itself swappable behind the stream contract
- **Epic(s):** #7 — payload promotion into `packages/aci-protocol` (overlaps the channel seam, landed)
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — opinionated core (`agentos:runs` stream), not one of the six swap jobs
- **ADR(s):** [ADR-0027](../../adr/0027-thin-broker-port-defer-second-broker.md) — the broker port at the non-sacred seams; [ADR-0007](../../adr/0007-adopt-not-build-boundaries.md) — adopt-not-build (Valkey adopted; second broker deferred)
