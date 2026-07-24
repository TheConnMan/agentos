# 31. Harness-neutral runner seams for the second harness

Date: 2026-07-11
Status: Superseded by [ADR-0060](0060-the-harness-is-a-declared-package.md) and [ADR-0061](0061-out-of-process-harness-boundary.md) (decisions 2/3/5 become manifest fields in 0060; decision 4 carries forward unchanged; decision 1 becomes 0061's recorded fallback)

## Context

ADR-0005 froze the ACI and made the claude-agent-sdk adapter the MVP harness behind the
`ModelSession` port. The OpenCode second-harness spike (#25, PR #226) proved that port: a live
`opencode serve`-backed session passed the frozen conformance suite on real model turns with zero
core changes. But it passed by paying a *synthesis tax* — the adapter forges claude-agent-sdk
dataclasses (including dummy `ResultMessage` fields nothing reads) because four seams around the
port are implicitly Claude-shaped: `translate.py`/`session.py` isinstance-match SDK types, the
side-effect allowlist hardcodes Claude Code's PascalCase tool names, plugin ingestion hands the
bundle straight to the SDK, and credential binding targets SDK env vars. A seam audit sized each
extraction by one test: does it shrink second-harness work, or only relocate it?

## Decision

1. **The runner owns its message model.** Replace the SDK dataclasses flowing out of
   `ModelSession.receive_turn` with a runner-owned `TurnEvent` union
   (`AssistantText | ToolCall | RateLimit | TurnResult`). The Claude adapter maps SDK→TurnEvent;
   other harnesses emit TurnEvent directly. `translate.py`, `otel.py` feeds, and budget tracking
   consume TurnEvent only. This is the one extraction that shrinks per-harness work.
2. **Tool identity is harness-declared.** Side-effect classification keys on a read-only tool set
   the harness declares, not the hardcoded Claude tool-name list. (Under OpenCode's lowercase tool
   names, every read-only tool currently misclassifies as side-effecting, wrongly suppressing the
   worker's auto-retry.)
3. **Bundle ingestion goes behind a `BundleInstaller` port** (validated bundle in → harness-ready
   session config out). The Claude implementation stays a passthrough; a non-Claude implementation
   is a bundle→native-config compiler and is budgeted as its own workstream, not as interface work.
4. **History/resume is descoped**, not abstracted. The consumer half exists
   (`CURIE_HISTORY_REF`→SDK resume) but nothing in production produces a history ref, and the
   frozen ACI `final` frame carries no session id. Building it starts with an `aci-protocol`
   contract change and its own ADR.
5. **No options abstraction.** `SessionConfig` (frozen in `aci-protocol`) is already the shared
   contract; each harness maps it to native options inline. A `build_options` port is speculative
   generality until a third harness exists.

## Alternatives considered

- **Keep the synthesis shim** (the spike's approach): works and proved the port, but makes every
  future harness depend on `claude_agent_sdk` types and forge fields nothing consumes.
- **Parallel translate modules per harness:** avoids the shared model but duplicates the
  translation/error/empty-result logic that should stay single-sourced.

## Consequences

- The frozen conformance suite is the refactor's safety net: TurnEvent lands with zero
  `aci-protocol` change and must keep `run_conformance` green for the Claude, fake, and OpenCode
  sessions alike.
- The under-priced item is named: the OpenCode bundle compiler (skills remap is near-free since
  OpenCode reads `.claude/skills/` natively; MCP/command/agent remaps are mechanical; bundle
  `scripts/` have no OpenCode target and are declared unsupported until a real bundle needs them).
- Parity evals remain the bar before any second harness is elevated past spike status (#25).
