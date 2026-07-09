# INTERFACE: Harness in-process (`ModelSession`)

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).
> **Kind:** CLEAN &nbsp;·&nbsp; **Implementations today:** 1 + fake &nbsp;·&nbsp; **Swap-readiness grade:** A- (the harness / ACI seam)

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

Inside the runner the model harness is reached through one in-process port: the
`ModelSession` Protocol. Everything above it (ACI translation, budget, side-effect
flagging, NDJSON, the HTTP layer) is written against the Protocol, so the concrete
`claude-agent-sdk` driver is the only SDK-coupled code. What stays opinionated core
is the frozen ACI wire contract the runner serves; the port is how a harness plugs
into that runner. Steer and interrupt are first-class Protocol operations, not
emulated.

## Current contract

A second harness must supply an object satisfying `ModelSession`
(`runner/src/agentos_runner/adapter.py:24`), a five-method `Protocol`:

- `async def connect(self) -> None` (`adapter.py:27`) — start/attach the harness,
  rehydrating if a resume ref is configured.
- `async def query(self, text: str) -> None` (`adapter.py:31`) — push a user message;
  a `query` issued while a turn is live is the mid-run **steer**.
- `def receive_turn(self) -> AsyncIterator[Any]` (`adapter.py:35`) — yield the harness
  messages for the current turn, ending at its terminal result.
- `async def interrupt(self) -> None` (`adapter.py:39`) — native hard stop at the next
  safe boundary.
- `async def close(self) -> None` (`adapter.py:43`) — tear down.

The messages a `receive_turn` iterator yields must be mappable by
`translate_message` (`runner/src/agentos_runner/translate.py:55`) into the ACI
outbound union (`TextDelta` / `ToolNote` / `SideEffectFlag` / `ErrorEvent` /
`Final`). Session options are assembled by `build_options`
(`adapter.py:48`), which pins `permission_mode="bypassPermissions"`
(`adapter.py:82`).

## Implementations today

Two, both in `runner/src/agentos_runner/`:

- **Real:** `ClaudeAgentSession` (`adapter.py:87`), wrapping `ClaudeSDKClient` in
  streaming-input mode; `receive_turn` delegates to `self._client.receive_response()`
  (`adapter.py:100`) and `interrupt` to `self._client.interrupt()` (`adapter.py:103`).
- **Fake:** `FakeModelSession` (`runner/src/agentos_runner/fake.py:52`), a scripted
  replayer that constructs real SDK message dataclasses. It is the reusable acceptance
  harness: `conformance_producer` (`runner/src/agentos_runner/conformance.py:33`) drives
  a real `SessionRunner` over the fake (`conformance.py:24`), so the ACI conformance gate
  validates the actual translation/final plumbing, not a canned stream.

## Known leakage

The port is CLEAN as a code interface but leaks harness shape in two places, both
called out in vision-doc Job 1:

- **SDK-shaped resume.** `AGENTOS_HISTORY_REF` is read verbatim into `history_ref`
  (`runner/src/agentos_runner/config.py:64`) and passed as the SDK `resume` session id
  by `build_options` (`adapter.py:64`, `:80`). A harness without an equivalent
  resume/session-store concept must build its own history store.
- **Plugin-format entanglement.** `packages/plugin-format` is the Claude Code plugin
  shape verbatim, so a non-Claude harness must interpret Claude Code plugin bundles or
  translate them; "implement the ACI server" understates that work.

## Cross-links

- **Epic(s):** — no standalone epic; folds into #25 (ACI producer / second-harness work).
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — Job 1 (Harness / runtime), grade A-.
- **ADR(s):** [ADR-0005](../../adr/0005-claude-agent-sdk-adapter-and-frozen-aci.md) — claude-agent-sdk adapter behind a frozen ACI session contract; [ADR-0011](../../adr/0011-opencode-second-harness.md) — OpenCode as the second harness behind the ACI.
