# 82. Connected-transport approval cards ride a real CLI-posted placeholder

Date: 2026-07-24

Status: Accepted

## Context

ADR-0078 adopted routing message-driven approval cards through the connected
Slack transport (issue #770), and specified the mechanism as: the CLI enqueues
with **no reply endpoint and an empty `reply_handle.placeholder`**, and the
worker (kernel + `slack_sink`) recognizes that empty-placeholder sentinel and
posts **top-level** instead of editing a placeholder. It called that a
sacred-kernel change requiring an `in_requesting_channel` rework.

Implementing it proved that mechanism **cannot work**: an empty placeholder is a
**frozen-contract violation**. `packages/aci-protocol`'s `ApprovalRequest`
constrains `reply_placeholder` to `min_length=1` (its test suite states the
intent: "ApprovalRequest string fields min_length=1 (adopt the API's strict
side)"). A turn carrying an empty placeholder is therefore rejected at
approval-create, and the kernel's pause path **escalates instead of posting a
card** — the exact opposite of #770's goal. This was caught by a kernel test
written against the sentinel design, before any of it shipped.

Representing "no placeholder" on the wire would mean an ACI protocol change
(`PROTOCOL_VERSION` bump, `wire.lock`, regenerated Rust + TS), raised as a
contract gap per AGENTS.md, **plus** the sacred-kernel rework. Both, to avoid
posting one Slack message.

## Decision

**In connected mode the CLI posts a REAL placeholder message over the connected
transport and enqueues the turn against that real `ts`.** No sentinel, no wire
change, no kernel change.

- `local`/`cluster message` detect a connected workspace — at cluster tier a
  `<release>-dispatcher` Deployment (`ops::dispatcher_connected`), at local tier
  a genuine `SLACK_BOT_TOKEN` rather than the `xoxb-dev` stub sentinel — and skip
  minting the in-process stub.
- They discover the workspace bot token (`CURIE_SLACK_BOT_TOKEN`, else the
  release Secret's `slackBotToken` at cluster tier / the persisted secret at
  local tier), `chat.postMessage` a real placeholder to the target channel, and
  enqueue with `reply_handle.placeholder` = that real `ts` and
  `reply_handle.endpoint` = `None`.
- The turn is then **indistinguishable from a dispatcher-originated one**, so the
  existing paths already do the right thing: the worker edits the real
  placeholder in place over its default (connected) transport, the
  requesting-channel approval card threads under it, and a resumed reply — even
  long after the CLI exited — edits that same real message, because a real `ts`
  supports `chat.update`.
- The CLI cannot observe a reply that lands in Slack, so it reports the enqueue
  and points the operator at the channel rather than waiting on a stub.
- The zero-Slack path (ADR-0063) is untouched: with no connected workspace the
  stub is still minted and kept alive exactly as today.

**This supersedes ADR-0078's mechanism** (its empty-placeholder sentinel, its
top-level-post rule, and its sacred-kernel `in_requesting_channel` rework).
ADR-0078's *intent* — the connected transport is the durable, clickable approval
surface, superseding ADR-0063's deferral of it — stands unchanged and is carried
forward here.

## Consequences

- **The change is CLI-only** (`cli/src/message.rs`, `cli/src/slack.rs`,
  `cli/src/ops.rs`). The kernel, `slack_sink`, and the ACI wire are untouched — a
  far smaller blast radius, and it needs no sacred-module review or contract-gap
  escalation.
- **The CLI now holds a workspace bot token** in connected mode, discovered the
  same masked way as the other release credentials and used only to post the
  placeholder; it is never printed.
- **One extra Slack API call per connected turn** (the placeholder post). That is
  the whole cost of avoiding a wire + kernel change.
- **A connected turn's reply is not visible in the CLI.** `message` confirms the
  enqueue and names the channel; `--dry-run` states the conditional (it cannot
  probe for a dispatcher without touching the network).
- **Real-Slack E2E is the acceptance gate**: unit tests cover detection,
  precedence, the dry-run plan, and the endpoint-less enqueue shape, but only a
  connected workspace proves the card renders, threads, resolves on click, and
  that the resumed reply edits the placeholder.

## Alternatives considered

1. **ADR-0078's empty-placeholder sentinel.** Rejected on a hard blocker: the
   frozen `min_length=1` constraint above means the pause escalates instead of
   posting a card. Would require an ACI wire change plus a sacred-kernel rework.
2. **Add an optional "no placeholder" field to `ReplyHandle`.** The honest
   version of alternative 1 — but still a frozen-contract change
   (`PROTOCOL_VERSION` bump + regen + contract-gap escalation) and still needs the
   kernel to learn a second reply mode. Posting one message from the CLI achieves
   the same outcome with neither.
3. **Keep the stub in connected mode and forward its replies into Slack.** Adds a
   CLI-resident relay for messages the workspace can already deliver itself, and
   leaves the resumed-reply-after-exit case broken (the stub dies with the
   process) — the very failure #770 exists to fix.
