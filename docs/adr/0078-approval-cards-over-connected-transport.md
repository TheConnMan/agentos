# 78. Route message-driven approval cards through the connected Slack transport

Date: 2026-07-23

Status: Accepted

## Context

ADR-0063 fixed the zero-Slack case of a `message`-driven approval: `agentos
local/cluster message` keeps its in-process `SlackStub` alive across the
approval-pending window and delivers the resumed reply on the same placeholder
(#766/#772). That ADR's Decision was explicit that the stub is the reply
surface **"not (yet) the connected Slack transport"**, and its "Alternatives
considered #1" named routing through the connected transport as the eventual
durable, clickable-card surface — **"not rejected, only deferred" to Follow-up
A**. #770 is that follow-up.

The deferral existed because the connected-transport path is a coordinated
change across a sacred boundary, not a passenger on the offline fix:

- **The requesting-channel card rides the trigger's transport.** In
  `apps/worker/src/agentos_worker/kernel.py` the `in_requesting_channel` branch
  posts the card over `qevent.reply_handle.endpoint` (the stub) when the card
  channel equals the requesting channel. This is a deliberate, test-pinned
  design from #451 (`apps/worker/tests/kernel/test_approval_lifecycle.py`,
  `test_pause_emits_a_confirm_intent_for_the_approval_card` and
  `test_routed_approval_cards_go_to_the_bound_channel`).
- **A late resumed reply cannot `chat.update` the stub's placeholder ts** in
  real Slack: that synthetic timestamp never existed there, so the resumed
  reply must post top-level instead of editing the placeholder.
- **`cluster message`'s `--force-wire` guard inverts.** Today it refuses to
  wire the deployed worker to the local stub when a `<release>-dispatcher`
  exists (a live workspace), to avoid hijacking replies. Under this decision a
  connected dispatcher is the *intended* transport, so the CLI must instead
  skip minting the stub and let the card and resumed reply ride that transport.

## Decision

**When `message` runs against a connected transport (a running dispatcher), the
CLI posts a REAL placeholder message to the card channel over that transport and
enqueues the turn against its real Slack ts** — so the approval card and the
resumed reply ride the connected Slack transport with no throwaway stub, on the
EXISTING kernel and sink paths.

- `local/cluster message` detects a connected dispatcher and, instead of minting
  the in-process stub, discovers the workspace's bot token (from the release
  Secret at cluster tier / the compose env at local tier, exactly as it already
  discovers the API key and Valkey password) and posts a real "thinking…"
  placeholder to the target channel via `chat.postMessage`.
- It enqueues the turn with `reply_handle.placeholder` = that REAL Slack ts and
  `reply_handle.endpoint` unset, so the turn looks exactly like a
  dispatcher-originated one: the worker edits the real placeholder in place over
  its default transport, and the requesting-channel approval card threads under
  it — all on the existing paths, unchanged.
- The resumed reply, arriving long after the CLI exited, edits that same real
  placeholder over the worker's default transport. `chat.update` succeeds
  because the ts is a real message — the exact thing a synthetic stub ts could
  not support, which is why the card/reply had to move off the stub.
- `cluster message`'s `--force-wire` guard is reworked: a connected dispatcher is
  now the intended transport, not a hijack to refuse; the stub-wiring path it
  guarded is not taken in connected mode.
- The zero-Slack path (ADR-0063) is unchanged: with no connected transport, the
  CLI still mints the stub and keeps it alive exactly as today.

Crucially this needs **no change to the sacred kernel and no change to the frozen
ACI wire**: because the placeholder is a real Slack ts, the existing
edit-in-place reply model and requesting-channel card threading already do the
right thing.

This **partially supersedes ADR-0063** — specifically its deferral of the
connected-transport surface ("not (yet) the connected Slack transport" /
Follow-up A). ADR-0063's offline keep-alive decision and its Postgres-durable
`Approval` record (ADR-0010) stand unchanged; approval semantics are untouched
(resolution stays an authorized `--resolve` or a real card click).

## Consequences

- **The change is CLI-only** (`cli/src/message.rs` plus credential discovery in
  `cli/src/ops.rs`): the CLI gains a real Slack `chat.postMessage` call and
  bot-token discovery mirroring its existing api-key / Valkey-password discovery.
  The kernel, the sink, and the ACI wire are untouched — a far smaller blast
  radius than the sacred-kernel + frozen-contract change first considered.
- **The CLI handles the workspace bot token** in connected mode, discovered the
  same masked way as the other release credentials and used only to post the
  placeholder; it is never printed.
- **It must be validated with real-Slack E2E** (a connected workspace): the
  placeholder posts, the card renders and threads under it, a click resolves, and
  the resumed reply edits the placeholder. Unit tests cover the detection, the
  argv/dry-run plan, and the enqueue shape; the live round trip is the gate.
- **Phasing (per the plan on #770).** This ADR lands first. The CLI change
  follows as its own PR, verified against a real workspace.

## Alternatives considered

1. **Keep it deferred (status quo).** Rejected: #770 is assigned and the
   connected-transport card is the durable, clickable surface operators expect
   when a workspace is connected; the offline stub is a fallback, not the goal.
2. **A standalone persistent reply server instead of the connected transport.**
   Already rejected in ADR-0063 (its Alternative 2): more infrastructure for a
   surface the connected workspace already provides.
3. **Worker posts top-level on an empty-placeholder sentinel** (the CLI enqueues
   endpoint-less with an empty `reply_handle.placeholder`, and the kernel/sink
   post top-level instead of editing). Rejected on a hard blocker: an empty
   `reply_placeholder` is a **frozen-contract violation** —
   `packages/aci-protocol`'s `ApprovalRequest.reply_placeholder` is `min_length=1`
   ("adopt the API's strict side"), so approval-create rejects the turn and the
   pause escalates instead of posting a card. Representing "no placeholder" would
   need an ACI wire change (PROTOCOL_VERSION bump + regen, raised as a contract
   gap per AGENTS.md) AND a sacred-kernel change. Posting a real placeholder from
   the CLI avoids both — no wire change, no kernel change — for a small,
   CLI-local cost.
