# INTERFACE: Queue / stream (Valkey)

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).
> **Kind:** CLEAN &nbsp;·&nbsp; **Implementations today:** 1 (redis-py) behind the broker port &nbsp;·&nbsp; **Swap-readiness grade:** not separately graded

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

The seam is the Valkey Stream wire contract between the dispatcher (producer) and the
worker (consumer): a named stream carrying a frozen one-field payload. As of #284 /
ADR-0027 the stream verbs are drawn behind a thin **broker port** at the two non-sacred
seams — a `StreamPublisher` `Protocol` on the producer (`apps/dispatcher/.../queue.py`:
`xadd` + the `SET NX EX` dedupe-claim) and a `StreamBroker` `Protocol` on the consumer
transport (`apps/worker/.../broker.py`: `xgroup_create`/`xreadgroup`/`xack`/`xautoclaim`).
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
  `DispatcherConfig.stream` (`apps/dispatcher/src/agentos_dispatcher/config.py:47`,
  env `AGENTOS_STREAM`) and `WorkerConfig.stream`
  (`apps/worker/src/agentos_worker/config.py:72`).
- **Payload encoding** — one Stream field, `STREAM_PAYLOAD_FIELD = "payload"`
  (`queue.py:31`), holding `model_dump_json()`. Produced by `enqueue` via
  `redis_client.xadd(config.stream, fields)`
  (`apps/dispatcher/src/agentos_dispatcher/queue.py:82`) and reconstructed by
  `QueuedSlackEvent.from_stream_fields` (`queue.py:60`).
- **Consumer verbs** — the worker reads with `xreadgroup` over a consumer group
  (`apps/worker/src/agentos_worker/consumer.py:104`), rebuilds the model at
  `consumer.py:143`, and acknowledges with `xack` (`consumer.py:160`). The group is
  `"agentos-workers"` (`WorkerConfig.consumer_group`, `config.py:73`).

Idempotency lives beside the stream, not in it: `claim_event` does a
`SET <dedupe_key> 1 NX EX <ttl>` before `XADD` (`queue.py:66`).

## Implementations today

One, redis-py against Valkey. The dispatcher `XADD`s (`queue.py:82`); the worker runs
a consumer group with `XREADGROUP`/`XACK` and crash-recovery `XAUTOCLAIM`
(`apps/worker/src/agentos_worker/consumer.py`). A second, sibling stream
`"agentos:evals"` uses the same one-field `payload` convention
(`apps/worker/src/agentos_worker/eval/stream.py`), reinforcing the wire shape as the
real contract.

## The port (as of #284 / ADR-0027)

Drawn only at the **non-sacred** seams; the sacred concurrency kernel
(`kernel.py`/`consumer.py`/`threadlock.py`/`markers.py`) is not touched:

- **Producer** — `StreamPublisher` (`apps/dispatcher/.../queue.py`): `xadd` and the
  `SET NX EX` dedupe-claim. `enqueue`/`claim_event` type against it.
- **Consumer transport** — `StreamBroker` (`apps/worker/.../broker.py`):
  `xgroup_create`/`xreadgroup`/`xack`/`xautoclaim`. The non-sacred `StreamConsumer`
  base (`stream_consumer.py`) holds a `StreamBroker`; the sacred `consumer.py` subclass
  inherits it unchanged (its `XAUTOCLAIM` reclaim now targets the port by inheritance).

The verbs return a bare `Awaitable`/value matching redis-py's own typing, so
`redis.asyncio.Redis` / `redis.Redis` satisfy the ports structurally with no adapter.

## Known leakage

- **Reclaim + composition root still touch redis directly.** `consumer.py`'s
  `XAUTOCLAIM` call (sacred file, by rule) and `run.py`'s client construction (the
  composition root, by design) reference redis directly; the API's `main.py` redis is
  the kill-switch / eval-queue, not the runs stream, and is out of scope. Fully unifying
  these is left for when a real second broker lands (ADR-0007, ADR-0027).
- The payload carried on this stream is `QueuedSlackEvent` — Slack-shaped by name
(`slack_event_id`, `thread_ts`, `placeholder_ts`). That vendor leakage is not the queue
seam's own; it belongs to the [channel / ingress seam](../channel-ingress/INTERFACE.md),
and #7 promotes the payload into `packages/aci-protocol` with channel-neutral names,
cleaning both seams at once. The queue seam's own constraint is narrower: the contract
assumes redis Stream semantics (ordered entries, consumer groups, pending-entry
reclaim), so a swap that stays redis-compatible is a URL change while a non-redis broker
rewrites the wire and the consumer verbs.

## Cross-links

- **Epic(s):** #85 — vision: make the broker itself swappable behind the stream contract
- **Epic(s):** #7 — payload promotion into `packages/aci-protocol` (overlaps the channel seam)
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — opinionated core (`agentos:runs` stream), not one of the six swap jobs
- **ADR(s):** [ADR-0027](../../adr/0027-thin-broker-port-defer-second-broker.md) — the broker port at the non-sacred seams; [ADR-0007](../../adr/0007-adopt-not-build-boundaries.md) — adopt-not-build (Valkey adopted; second broker deferred)
