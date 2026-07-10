# INTERFACE: Channel / ingress (Slack)

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).
> **Kind:** SOFT &nbsp;·&nbsp; **Implementations today:** 1 &nbsp;·&nbsp; **Swap-readiness grade:** C

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

The line that makes the communication channel swappable is the pair of contracts at
the two ends of the run: the ingress payload the dispatcher enqueues
(`QueuedSlackEvent`) and the egress port the kernel writes replies through
(`SlackSink`). Everything between them — routing, concurrency, sandboxing — is
opinionated core and channel-agnostic. This is the least-clean of the AgentOS seams:
both ends are Slack-shaped by name and by semantics, so a second channel does not
drop in behind a stable interface today. One implementation today; the port is the
wire + Protocol contract, not a channel-neutral code interface — extracted only when a
second channel demands it ("the second implementation teaches the interface").

## Current contract

A second channel must produce the ingress payload and satisfy the egress Protocol:

- **Ingress** — `QueuedSlackEvent`, promoted into the frozen contract at
  `packages/aci-protocol/src/aci_protocol/queue.py` (#7, ADR-0020); the dispatcher
  subclass (`apps/dispatcher/src/agentos_dispatcher/queue.py`) only adds the stream
  transport. Still Slack-named fields (the channel-neutral rename is PR-B):
  `slack_event_id` (idempotency key), `thread_ts` (conversation key), `channel`, `user`,
  `text`, and `placeholder_ts` (the pre-posted reply the worker edits in place,
  line 53). It serializes to a single Stream field via `to_stream_fields`
  (`queue.py:56`), keyed by `STREAM_PAYLOAD_FIELD = "payload"` (`queue.py:31`).
- **Egress** — the `SlackSink` Protocol (`apps/worker/src/agentos_worker/slack_sink.py:21`),
  whose core method is `async def update(self, *, channel: str, ts: str, text: str)`
  (`slack_sink.py:24`) — an edit-in-place on Slack's `chat.update`, plus best-effort
  `set_status`/`clear_status`. The mrkdwn dialect is confined behind the sink in
  `to_mrkdwn` (`apps/worker/src/agentos_worker/mrkdwn.py:45`).
- **Binding** — a channel resolves to a deployment by `agents.slack_channel`
  equality in `BindingResolver.resolve` (`apps/worker/src/agentos_worker/binding.py:94`).

## Implementations today

One: Slack. Ingress is `apps/dispatcher` (Bolt / Socket Mode); egress is
`AsyncSlackSink` (`slack_sink.py:37`) on the Slack Web API. The swap proof that the
protocol (not just the service) is the seam: the Rust CLI mints the exact
`QueuedSlackEvent` wire payload — using the generated `agentos_aci_protocol::QueuedSlackEvent`
(`cli/src/queue.rs`) — and drives the whole deployed system with zero Slack contact
via `agentos local message` / `cluster message` (`cli/src/chat.rs`, `cli/src/message.rs`).

## Known leakage

The line leaks the vendor through the core contract in three ways. (1) The ingress
field names are Slack's: `slack_event_id`, `thread_ts`, `placeholder_ts`. (2) The
egress semantics are edit-a-placeholder — `update(channel, ts, text)` on `chat.update`,
not post-a-message — so any channel without in-place edit must emulate it. (3) The
reply base URL is worker-global: `WorkerConfig.slack_api_base_url`
(`apps/worker/src/agentos_worker/config.py:40`), fed to `AsyncSlackSink(base_url=...)`
(`slack_sink.py:45`), so two ingress paths cannot coexist on one deployment until
replies route per turn. The restraint is deliberate: no multi-channel adapter framework
is built (#27) — the channel-neutral rename comes with the second real channel.

## Cross-links

- **Epic(s):** #7 — promote the queue payload into `packages/aci-protocol` with
  channel-neutral field names (Stage A promotion landed, ADR-0020; the
  channel-neutral rename is the pending PR-B)
- **Epic(s):** #19 — per-turn reply routing (the reply base URL is worker-global today)
- **Epic(s):** #27 — deliberately defers a pluggable multi-channel framework
- **Epic(s):** #38 — channel-seam hardening / follow-up
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — Job 6 (Communication channel), grade C
- **ADR(s):** none directly on this seam
