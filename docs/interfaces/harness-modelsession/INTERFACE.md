---
seam: Harness in-proc / ModelSession
kind: CLEAN
impls: 1 + fake
grade: A-
vision_row: Harness / runtime
epics:
  - "#25"
order: 2
epic_note: folds into
---
# INTERFACE: Harness in-process (`ModelSession`)

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).
<!-- BEGIN GENERATED: header (agentos dev docs-lint) -->
> **Kind:** CLEAN &nbsp;·&nbsp; **Implementations today:** 1 + fake &nbsp;·&nbsp; **Swap-readiness grade:** A-
<!-- END GENERATED: header -->

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

Inside the runner the model harness is reached through one in-process port: the
`ModelSession` Protocol. Everything above it (ACI translation, budget, side-effect
flagging, NDJSON, the HTTP layer) is written against the Protocol. The port itself is
CLEAN, but the SDK is not yet confined to one module: eight runner modules still import
`claude_agent_sdk` today (`check.py`, `session.py`, `hooks.py`, `adapter.py`, `fake.py`,
`approval.py`, `translate.py`, `plugin.py`), and the value that crosses the port is
currently the raw SDK message union rather than a runner-owned neutral type. The
runner-owned `TurnEvent` model that would draw that line is pending (#307/#315, both
open); until it lands, a second harness must emit objects the SDK-shaped translation
step accepts. What stays opinionated core is the frozen ACI wire contract the runner
serves; the port is how a harness plugs into that runner. Steer and interrupt are
first-class Protocol operations, not emulated.

## Current contract

A second harness must supply an object satisfying `ModelSession`
(`runner/src/agentos_runner/adapter.py::ModelSession`), a five-method `Protocol`:

- `async def connect(self) -> None` (`runner/src/agentos_runner/adapter.py::ModelSession.connect`) — start/attach the harness,
  rehydrating if a resume ref is configured.
- `async def query(self, text: str) -> None` (`runner/src/agentos_runner/adapter.py::ModelSession.query`) — push a user message;
  a `query` issued while a turn is live is the mid-run **steer**.
- `def receive_turn(self) -> AsyncIterator[Any]` (`runner/src/agentos_runner/adapter.py::ModelSession.receive_turn`) — yield the harness
  messages for the current turn, ending at its terminal result.
- `async def interrupt(self) -> None` (`runner/src/agentos_runner/adapter.py::ModelSession.interrupt`) — native hard stop at the next
  safe boundary.
- `async def close(self) -> None` (`runner/src/agentos_runner/adapter.py::ModelSession.close`) — tear down.

The messages a `receive_turn` iterator yields must be mappable by
`translate_message` (`runner/src/agentos_runner/translate.py::translate_message`) into the ACI
outbound union (`TextDelta` / `ToolNote` / `SideEffectFlag` / `ErrorEvent` /
`Final`). Today those messages are the concrete `claude_agent_sdk` dataclasses; the
neutral `TurnEvent` payload that would decouple the port from the SDK shape is #307/#315,
not yet shipped. Session options are assembled by `build_options`
(`runner/src/agentos_runner/adapter.py::build_options`). Since #245 / ADR-0010 the permission
posture is conditional, not pinned: with an approval `can_use_tool` callback the session
runs in `permission_mode="default"` so each tool call is gated, and only an unconfigured
agent (no callback) keeps the historical `"bypassPermissions"` verbatim
(`runner/src/agentos_runner/adapter.py::build_options`).

## Implementations today

Two, both in `runner/src/agentos_runner/`:

- **Real:** `ClaudeAgentSession` (`runner/src/agentos_runner/adapter.py::ClaudeAgentSession`), wrapping `ClaudeSDKClient` in
  streaming-input mode; `receive_turn` delegates to `self._client.receive_response()`
  (`runner/src/agentos_runner/adapter.py::ClaudeAgentSession.receive_turn`) and `interrupt` to `self._client.interrupt()`
  (`runner/src/agentos_runner/adapter.py::ClaudeAgentSession.interrupt`).
- **Fake:** `FakeModelSession` (`runner/src/agentos_runner/fake.py::FakeModelSession`), a scripted
  replayer that constructs real SDK message dataclasses. It is the reusable acceptance
  harness: `conformance_producer` (`runner/src/agentos_runner/conformance.py::conformance_producer`) drives
  a real `SessionRunner` over the fake (`runner/src/agentos_runner/conformance.py::_build_runner`), so the ACI conformance gate
  validates the actual translation/final plumbing, not a canned stream.

## Known leakage

The port is CLEAN as a code interface but leaks harness shape where the SDK is not yet
walled off, called out in vision-doc Job 1:

- **SDK-shaped message payload.** The value crossing the port is the concrete
  `claude_agent_sdk` message union, and `claude_agent_sdk` is imported across eight
  runner modules rather than one adapter. The runner-owned `TurnEvent` model that draws
  the neutral line is #307/#315 (open); until it merges, a second harness emits SDK-shaped
  dataclasses that `translate_message` understands.
- **Plugin-format entanglement.** `packages/plugin-format` is the Claude Code plugin
  shape verbatim, so a non-Claude harness must interpret Claude Code plugin bundles or
  translate them; "implement the ACI server" understates that work.

## Cross-links

- **Epic(s):** — no standalone epic; folds into #25 (ACI producer / second-harness work).
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — Job 1 (Harness / runtime), grade A-.
- **ADR(s):** [ADR-0005](../../adr/0005-claude-agent-sdk-adapter-and-frozen-aci.md) — claude-agent-sdk adapter behind a frozen ACI session contract; [ADR-0010](../../adr/0010-approval-gates-and-human-in-the-loop.md) — approval gates make `permission_mode` conditional on a `can_use_tool` callback; [ADR-0011](../../adr/0011-opencode-second-harness.md) — OpenCode as the second harness behind the ACI.
