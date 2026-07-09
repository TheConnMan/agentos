# INTERFACE: Queue / stream (Valkey)

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).
> **Kind:** SOFT &nbsp;·&nbsp; **Implementations today:** 1 (redis-py) &nbsp;·&nbsp; **Swap-readiness grade:** not separately graded

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

The seam is the Valkey Stream wire contract between the dispatcher (producer) and the
worker (consumer): a named stream carrying a frozen one-field payload, spoken over the
redis-py client. This is mostly opinionated **core** — the routing, consumer-group
concurrency, dedupe, and reclaim rules are AgentOS's own and not meant to be swapped —
with a soft swap only at the broker level: any redis-compatible Stream backend (Valkey,
Redis, a managed equivalent) is a URL change. A non-redis broker (Kafka, SQS) would
touch the wire and is a rewrite, not a config swap. One implementation today; the port
is the stream key + payload shape + redis-py Stream verbs, not a code interface.

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

## Known leakage

The payload carried on this stream is `QueuedSlackEvent` — Slack-shaped by name
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
- **ADR(s):** none directly on this seam
