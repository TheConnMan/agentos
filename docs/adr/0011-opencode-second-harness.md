# 11. OpenCode as the second harness behind the ACI

Date: 2026-07-09
Status: Accepted (gating steer spike done — ADR-0031, #25/PR #226)

## Context

The ACI is the frozen, tri-language, session-scoped contract between the platform
and whatever runs a bundle: stream NDJSON events, steer mid-run, interrupt
(ADR-0005). It has exactly one implementation, the claude-agent-sdk runner. The
second implementation is what proves the port and de-risks the on-prem
harness-swap promise. We intend OpenCode (`sst/opencode`) to be that second
harness: it is the direction the ecosystem is moving and it brings capabilities
we would otherwise build ourselves.

Investigation of OpenCode against the primitives AgentOS relies on found that it
ships most of them as first-class configuration - but with two consequences that
make this a genuine architectural fork, not a drop-in.

## Decision

Adopt **OpenCode as a second ACI harness, contingent on a steer spike**, and
keep the claude-agent-sdk adapter as a fallback for steer-dependent agents until
steer parity is proven.

- **Gate:** before committing, spike `opencode serve` driven as an ACI streaming
  server and determine whether mid-turn message injection (steer) is achievable
  at a tool-loop boundary, or whether OpenCode can only offer
  abort-and-resend-with-carried-context. This decides whether OpenCode is a
  *full* ACI implementation or a *degraded-steer* one.
- Build a **`bundle -> opencode.jsonc` translator** at deploy/runtime time. The
  bundle stays the Claude Code plugin shape verbatim (ADR-0005, the distribution
  wedge); OpenCode never sees the Claude manifest, only the translated config.
- Where OpenCode provides a capability natively (MCP `{env:}` secrets, MCP OAuth,
  allow/ask/deny approvals, id-addressable session resume), prefer it over a
  bespoke platform build; the corresponding platform ADRs (per-agent secrets,
  approval gates, durable multi-turn) narrow to the platform-side delivery only.

## Evidence (docs review, opencode.ai/docs, 2026-07-09)

- First-class and directly usable: native Skills (it even reads the
  `.claude/skills/` path); stdio and remote MCP with `{env:VAR}` secret
  interpolation and RFC 7591 dynamic client registration OAuth; a blocking
  `tool.execute.before` hook; subagents; allow/ask/deny permissions where `ask`
  pauses for human approval; id-addressable session persistence and resume; a
  headless `opencode serve` HTTP server with an SSE event stream and a
  `POST /session/:id/abort` interrupt; model-agnostic providers.
- **Two gaps, both predicted by the second-harness analysis.** (1) OpenCode does
  not load a Claude plugin bundle verbatim: `.claude-plugin/plugin.json` and
  Claude's `.mcp.json` are ignored (MCP must live under OpenCode's own `mcp` key),
  so a translator is required even though SKILL.md payloads port cleanly. (2)
  Mid-run **steer** is not a shipped API primitive - OpenCode offers stream and
  interrupt cleanly, but injecting a message into an in-flight turn exists only as
  TUI message-queuing plus open feature work.

## Alternatives considered and rejected

1. **Stay single-harness on the claude-agent-sdk.** Rejected as the long-term
   stance: it locks model and vendor choice and contradicts the model-agnostic,
   opinionated-core / swappable-jobs commitment. Retained only as the fallback
   adapter for steer-heavy agents.
2. **Strands Agents SDK as the second harness** (the prior front-runner). Strong
   stream/steer/interrupt fit and async throughout. Not chosen because OpenCode is
   the ecosystem-standard target and brings the free MCP-OAuth, approvals, and
   resume; Strands is the revisit candidate if OpenCode's steer gap proves fatal.
3. **Google ADK.** Rejected: its request/response server has no steer or interrupt
   endpoint, and the bidirectional `run_live` stack is voice/Live-API-tuned and
   off the documented path for a text NDJSON contract.
4. **Build our own harness.** Rejected: enormous, and it contradicts the
   adopt-not-build boundary (ADR-0007). The ACI exists precisely so we adopt an
   engine behind it.
5. **Translate bundles to an OpenCode-native format at author time.** Rejected:
   it breaks "the bundle is the Claude Code plugin shape verbatim" (ADR-0005). The
   translation must be a deploy/runtime concern, invisible to the author, or the
   distribution wedge is lost.
6. **Accept degraded steer (abort-and-resend) unconditionally.** Deferred to the
   spike, not accepted blind: the worker's finish-race kernel invariant is built
   on steer-at-a-tool-boundary, so degraded steer may weaken a correctness
   property. The spike decides whether that cost is acceptable or whether steer
   parity must be driven upstream first.

## Consequences

- This decision gates the *implementation choices* of the per-agent-secrets and
  approval-gate ADRs: several of their hardest parts arrive with the harness
  rather than being built on the Claude SDK, so their build work should wait on
  this fork.
- The bundle translator, not the ACI server, is the bulk of the work (consistent
  with the second-harness analysis).
- Steer is the standing risk. The likely near-term shape is a dual-harness period:
  OpenCode as the default, the claude-agent-sdk adapter retained for
  steer-dependent agents until parity.
- If the spike shows steer is unreachable and the degradation is unacceptable,
  this ADR is superseded by one that either keeps a single harness or selects
  Strands.
