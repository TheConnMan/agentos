# INTERFACE: Channel interaction

> Part of the AgentOS swappable-seam catalog. **Kind:** CLEAN. **Version:** 1.0.

## The black line

Agents produce a semantic `OutboundMessage`; channel adapters render it. Slack
may use Block Kit and the terminal may use a numbered selector, but neither
widget appears in the contract. This is the interaction half of ADR-0020.

The source of truth is
`packages/channel-protocol/src/channel_protocol/models.py`; the committed JSON
Schema is `packages/channel-protocol/schema/channel-protocol.schema.json`.

## Message contract

Every message has `version: "1.0"` and mandatory `text`. The text must be a
complete usable reply after all optional fields are removed. Optional `status`,
`header`, `fields`, `links`, and `footer` enrich presentation.

An optional `interaction` is one semantic intent:

- `choice`: an id, optional prompt, one to ten `{label, value}` options, and
  `allow_free_text` (default `true`).
- `confirm`: an id, prompt, semantic confirm and cancel actions, and
  `allow_free_text` (default `false`).

Action `label` is display text. Action `value` is the exact inbound message sent
when selected. Values are conversation input, not trusted authorization tokens;
the server must still authorize side effects and approvals.

## ACI envelope

Until ACI gains a native semantic-message event, a runner carries the message
inside its final text as a complete fenced block:

````text
```agentos-reply
{"version":"1.0","text":"Which view?","interaction":{"kind":"choice","id":"view","options":[{"label":"Open issues","value":"show open issues"}]}}
```
````

Adapters must hide incomplete or malformed envelopes and fall back to ordinary
text. The legacy unversioned `buttons: [[label, value]]` shape remains readable
during migration but is not valid v1 authoring.

## Adapter requirements

- Render `text` even when no optional capability is supported.
- Advertise capabilities; never infer them from the channel name.
- Render choices and confirmations using native affordances when
  `interactive-actions` is present and as numbered text otherwise.
- Preserve action values exactly when converting a selection into inbound text.
- Keep rendering and channel-native payloads inside the adapter.
- Treat links as navigation, not conversation responses.
- Never grant authority based only on an action id or value.

## TUI behavior

The AgentOS TUI advertises `interactive-actions`, `live-steering`, `streaming`,
and `threading`. It renders agent-authored actions first and appends `Type a
message...` as the final selector option when free text is allowed. Selecting
that option enters an explicit compose mode; it is a terminal affordance and is
never added to the agent-authored contract. The selector expands to keep every
contract option and the appended free-response option visible. The compose
field is hidden until free response is selected. The TUI sends the selected
action value or composed text as the next turn, replaces stale actions after
each reply, and never prints protocol fences or terminal status frames.

The conversation transcript remains scrollable while selecting, composing, and
waiting for a response. Scrolling suspends tail-follow until the user returns to
the latest output. Transcript navigation is adapter state and must not alter the
outbound message or its interaction intent.

Starter prompts are bundle metadata (`starterPrompts`), not response actions and
not hardcoded into the TUI. They disappear after the first turn unless the agent
returns a new interaction.
