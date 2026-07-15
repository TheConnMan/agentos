# 20. The message port: a rendering-free channel interface with capability negotiation

Date: 2026-07-11
Status: Accepted

## Context

ADR-0012 fixed the invariant that the runner never learns its channel, but it
implements the channel seam today as a single Slack base URL with a CLI stub
minting the Slack wire payload. That is one adapter pretending to be the
abstraction. ADR-0012 deliberately deferred the *shape* of the port; the moment a
second channel is real (email, Microsoft Teams, GitHub issue comments, a
first-class CLI/UI), the deferred question comes due.

The question: what is the shape of the channel port such that Slack can be rich
(threads, buttons, interactive actions) without either making low-capability
channels impossible or forcing every channel down to the lowest common
denominator?

An agent, squinted at, receives a message and produces a message. That framing
makes the port look obvious and then leaks in both directions:

- A **fat port** (every adapter must implement threads + buttons + actions)
  excludes email, Teams, and GitHub, and forces stubs the day a low-capability
  channel is real.
- A **thin port** (text in, text out) throws away the interactivity that makes an
  agent useful. ADR-0010's approval gates need a decision *collected* from a
  human; "a decent agent needs interactivity" is the whole point.
- A skill that emits Slack blocks directly **hard-codes the connection that
  bypasses the port**, which is exactly the coupling ADR-0012 exists to prevent,
  and its failure mode is silent (Docker-mode and CLI-stub tests still pass).

This is cross-cutting: it constrains what a skill may emit, it defines the
rendering-agnostic primitive the approval interface (ADR-0010) uses to present and
collect a decision, and it changes how the delivery model (ADR-0013) treats
channels that cannot steer a live run.

## Decision

The channel port is a **semantic, rendering-free interface**. The core and skills
produce a semantic message and query capabilities; each adapter renders
per-channel. The interface is what; the rendering is how, and how lives only in
the adapter. Four parts:

1. **Required core (every adapter implements).** Receive an inbound message (text,
   author, correlation key, attachments); send a semantic `OutboundMessage` whose
   `text` fallback is mandatory and always renders; advertise a capability set.

2. **Capability negotiation, not interface narrowing.** Adapters *declare* the
   optional capabilities they support (`INTERACTIVE_ACTIONS`, `LIVE_STEERING`,
   `STREAMING`, `RICH_CARDS`, `THREADING`, `FILE_ATTACHMENTS`). The core queries
   the capability set; it never assumes one. Optional capabilities are part of the
   interface as declared, queryable features, not as methods every adapter must
   implement.

3. **Semantic interaction intents, rendered not widget-authored.** The port
   carries interaction primitives (`Confirm`, `Choice`, and later `Form`), not
   buttons. Slack renders `Choice` as buttons, Teams as an Adaptive Card action
   set, email as "reply with 1 or 2" or approve links, GitHub as checkboxes or a
   slash-command, CLI as a numbered prompt. **The interaction is the port; the
   widget is the adapter.** This is the rendering-agnostic primitive the approval
   interface (ADR-0010) consumes to present and collect a human decision on any
   channel; approvals do not re-implement Slack buttons per agent.

4. **Native escape hatch, never load-bearing.** An adapter-specific
   `native_payload` (the Bot Framework `channelData` analogue) is permitted for
   genuinely native features the semantic model cannot express, but it is strictly
   additive: a message must render acceptably with it removed, gated by test.

Three house rules are locked by this ADR:

- **Progressive enhancement is mandatory.** Skills author the text/summary floor
  first; cards, buttons, and interaction affordances are additive enrichments.
  Correctness never depends on an enrichment existing. This is design-for-the-floor
  and enhance-upward, not design-Slack-rich-then-strip (which always bakes Slack
  assumptions into the core).
- **Rendering lives only in the adapter.** The core, the kernel, and skills must
  never import a channel SDK or emit channel-native markup (no `blocks: [...]`
  above the seam). This is ADR-0012's anti-coupling rule made concrete for the
  outbound path, and it is lint/CI-gateable.
- **Live steering is a capability, not an assumption.** ADR-0013's finish-race
  steering applies to channels that advertise `LIVE_STEERING` (Slack, CLI).
  Turn-based channels (email, GitHub comments) route each inbound as a fresh turn
  through the suspend/resume path (ADR-0003); the delivery model must not assume a
  live socket.

## Prior art

The shape above is the industry-convergent one, reached independently by four
mature systems:

- **Microsoft Bot Framework**: a channel-agnostic `Activity` schema, a
  `channelData` escape hatch for native per-channel features, and a card `summary`
  text that renders when a channel cannot render the card.
- **Adaptive Cards**: per-element `requires: <feature, version>` plus a cascading
  `fallback` (drop the element, or fall back to an ancestor's fallback), so one
  card authored once renders the richest subset each host supports.
- **Twilio Content API**: "the richest format the recipient supports," one
  template carrying `twilio/quick-reply` and `twilio/text` so WhatsApp gets buttons
  and SMS gets the text fallback from the same call.
- **Fowler's Role Interface / Interface Segregation**: a required core plus
  optional role interfaces beats a fat header interface that forces every
  implementer to implement everything.

All four land on the same three moves this ADR adopts: required-core plus optional
advertised capabilities, author-once and render-per-adapter with a declared
fallback, and a native escape hatch that is never load-bearing.

## Alternatives considered and rejected

1. **Lowest-common-denominator port (text in, text out).** Rejected: it throws
   away interactivity; ADR-0010 approvals cannot collect a decision, and the
   interactivity that distinguishes an agent from a log tailer is gone.
2. **Fat, rich port (every adapter implements threads, buttons, actions).**
   Rejected: it excludes email, Teams, and GitHub, forces capability stubs, and
   the abstraction leaks the day a low-capability channel is real.
3. **Skills emit channel-native markup (Slack blocks); the core translates down.**
   Rejected: it hard-codes the connection that bypasses the port (ADR-0012's named
   failure mode) and silently couples the core to Slack.
4. **Widget-level interaction primitives (a `Button` type) rather than semantic
   (`Choice`/`Confirm`).** Rejected: a button has no meaning on a channel without
   buttons; a semantic intent degrades gracefully, a widget cannot.
5. **Defer the whole thing until a second channel is real.** Rejected: the
   approval interface (ADR-0010) needs the rendering-agnostic interaction
   primitive now, and every skill written against Slack blocks in the interim is
   debt that has to be unwound.

## Consequences

- The `OutboundMessage` semantic type and the capability set become a versioned
  contract that skills author against; a skill emitting channel-native markup is a
  design error, gate-able in CI.
- The approval interface (ADR-0010) builds on the `Choice`/`Confirm` intent rather
  than Slack buttons. Route bindings choose the adapter; the interaction primitive
  stays constant across channels.
- ADR-0012's channel seam graduates from "a Slack base URL" to a real
  multi-adapter port; the CLI stub becomes one adapter among several rather than a
  Slack impersonator.
- The delivery model (ADR-0013) must treat `LIVE_STEERING` as conditional.
  Turn-based channels exercise suspend/resume (ADR-0003) as their normal path, not
  an exception, so the resume path's finish-race correctness becomes load-bearing
  for more than just Slack.
- New channels (email, Teams, GitHub, a first-class CLI/UI) are additive:
  implement the core, advertise capabilities, render the intents. No core change.
- The cheapest first increment is the one `architecture-vision.md` already names
  for the grade-C communication seam: promote the `QueuedSlackEvent` queue payload
  into `packages/aci-protocol` with channel-neutral field names (issue #7), which
  turns the Slack-shaped ingress contract into the required core of this port in a
  single change. This ADR does not require the full pluggable channel registry up
  front; the interaction-primitive half is driven now by the approval interface
  (ADR-0010), and the rest lands as real channels arrive.
- The exact v1 capability enum and `Choice` / `Confirm` intent fields are pinned
  in `packages/channel-protocol` and documented in the channel-interaction
  interface. `Form` remains deferred until an agent needs it.
