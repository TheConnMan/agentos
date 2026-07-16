---
seam: Channel / ingress (Slack)
kind: SOFT
impls: 1
grade: C
epics:
  - "#7"
  - "#19"
  - "#27"
  - "#38"
order: 4
---
# INTERFACE: Channel / ingress (Slack)

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).
<!-- BEGIN GENERATED: header (agentos dev docs-lint) -->
> **Kind:** SOFT &nbsp;·&nbsp; **Implementations today:** 1 &nbsp;·&nbsp; **Swap-readiness grade:** C
<!-- END GENERATED: header -->

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

The line that makes the communication channel swappable is the pair of contracts at
the two ends of the run: the ingress payload the dispatcher enqueues (`QueuedTurn`) and
the egress port the kernel writes replies through (`SlackSink`). Everything between them —
routing, concurrency, sandboxing — is opinionated core and channel-agnostic. Since #7 and
#19 the ingress payload and the per-turn reply routing are channel-neutral, so this is no
longer the least-clean seam by its wire contract; the remaining vendor shape is on the
egress semantics (edit-in-place) and on the Slack-typed binding surface. One implementation
today; the port is the wire + Protocol contract, extracted further only when a second
channel demands it ("the second implementation teaches the interface").

## Current contract

A second channel must produce the ingress payload and satisfy the egress Protocol:

- **Ingress** — `QueuedTurn` (`packages/aci-protocol/src/aci_protocol/turn.py::QueuedTurn`),
  a Pydantic model in the frozen ACI package with channel-neutral fields: `event_id`
  (idempotency key), `conversation_id` (the conversation/thread key routing keeps one live
  session per), `author`, `text`, `received_at`, and `reply_handle` — a `ReplyHandle`
  (`packages/aci-protocol/src/aci_protocol/turn.py::ReplyHandle`) carrying `channel`,
  `placeholder` (the pre-posted reply the worker edits in place), and an optional per-turn
  `endpoint`. The dispatcher serializes it to a single Stream field via `to_stream_fields`
  (`apps/dispatcher/src/agentos_dispatcher/queue.py::to_stream_fields`), keyed by
  `STREAM_PAYLOAD_FIELD = "payload"` (`apps/dispatcher/src/agentos_dispatcher/queue.py::STREAM_PAYLOAD_FIELD`).
  For the Slack adapter, `event_id` is the Slack event id, `conversation_id` is the thread
  ts, `author` is the Slack user id, and `reply_handle` carries the Slack channel plus the
  placeholder ts.
- **Egress** — the `SlackSink` Protocol (`apps/worker/src/agentos_worker/slack_sink.py::SlackSink`),
  whose core method is `async def update(self, *, channel: str, ts: str, text: str)`
  (`apps/worker/src/agentos_worker/slack_sink.py::SlackSink.update`) — an edit-in-place on Slack's `chat.update`, plus best-effort
  `set_status`/`clear_status`. The mrkdwn dialect is confined behind the sink in
  `to_mrkdwn` (`apps/worker/src/agentos_worker/mrkdwn.py::to_mrkdwn`).
- **Binding** — a channel resolves to a deployment by `agents.slack_channel`
  equality in `BindingResolver.resolve` (`apps/worker/src/agentos_worker/binding.py::BindingResolver.resolve`).

## Implementations today

One: Slack. Ingress is `apps/dispatcher` (Bolt / Socket Mode); egress is
`AsyncSlackSink` (`apps/worker/src/agentos_worker/slack_sink.py::AsyncSlackSink`) on the Slack Web API. The swap proof that the
protocol (not just the service) is the seam: the Rust CLI mints the exact
`QueuedTurn` wire payload with the same channel-neutral fields
(`cli/src/queue.rs`) and drives the whole deployed system with zero Slack contact
via `agentos local message` / `cluster message` (`cli/src/chat.rs`, `cli/src/message.rs`).

## Known leakage

Two ends were cleaned and one Slack surface is newly documented.

- **Fixed (#7).** The ingress field names were Slack's (`slack_event_id`, `thread_ts`,
  `placeholder_ts`); the payload was promoted into `packages/aci-protocol` as `QueuedTurn`
  with channel-neutral names.
- **Fixed (#19).** The reply base URL was worker-global; per-turn reply routing now rides
  `ReplyHandle.endpoint`, so a real Slack workspace and a no-Slack CLI stub can coexist on
  one deployment. `WorkerConfig.slack_api_base_url` (`apps/worker/src/agentos_worker/config.py::WorkerConfig`)
  is now only the default when a turn sets no `endpoint`, fed to `AsyncSlackSink`
  (`apps/worker/src/agentos_worker/slack_sink.py::AsyncSlackSink.__init__`).
- **Still leaks — egress semantics.** The reply model is edit-a-placeholder —
  `update(channel, ts, text)` on `chat.update`, not post-a-message — so any channel without
  in-place edit must emulate it.
- **Still leaks — the Slack-typed binding surface, undocumented until now.** The agents table
  carries a `slack_channel` column (`apps/api/src/agentos_api/models.py::Agent`), and agent
  create/update validate it as a Slack channel id via `_validate_slack_channel_id`
  (`apps/api/src/agentos_api/schemas.py::_validate_slack_channel_id`) wired onto
  `apps/api/src/agentos_api/schemas.py::AgentCreate` and
  `apps/api/src/agentos_api/schemas.py::AgentUpdate`. This is the largest remaining Slack
  surface and appears in no other seam doc: the binding key and its validators are
  Slack-shaped in the control plane, not just at the channel edges. The restraint is
  deliberate: no multi-channel adapter framework is built (#27) — the channel-neutral
  binding rename comes with the second real channel.

## Cross-links

- **Epic(s):** #7 — promote the queue payload into `packages/aci-protocol` with
  channel-neutral field names (landed)
- **Epic(s):** #19 — per-turn reply routing (landed)
- **Epic(s):** #27 — deliberately defers a pluggable multi-channel framework
- **Epic(s):** #38 — channel-seam hardening / follow-up
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — Job 6 (Communication channel), grade C
- **ADR(s):** none directly on this seam
- **Interaction contract:** [Channel interaction](../channel-interaction/INTERFACE.md)
  defines the semantic reply before this Slack adapter renders it.
