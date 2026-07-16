# 5. claude-agent-sdk adapter behind a frozen ACI session contract

Date: 2026-07-04
Status: Accepted

**Amended by [ADR-0036](0036-aci-semver-and-reader-policy.md)**
(back-link added under [ADR-0045](0045-the-status-line-is-the-mutable-part-of-an-immutable-adr.md)):
0036 amends the frozen-ACI posture below with semver, a reader-policy asymmetry,
and a wire-lock gate. This ADR remains the record of the freeze itself.

## Context

Everything inside the runtime boundary is "the harness"; everything outside is "the platform." For the platform to be harness-agnostic (and for the plugin format to be the distribution wedge), the seam between them must be an explicit, versioned contract — the ACI (Agent Container Interface). The MVP harness is Anthropic's claude-agent-sdk, whose streaming-input mode is what makes Claude-Code-style steering possible server-side. The open questions were whether the SDK can be driven as a long-lived server that accepts mid-run input and interrupt, and whether the plugin format loads natively.

## Decision

The ACI is a **session-scoped, bidirectional** contract (inject message / steer / interrupt / stream NDJSON / budget / side-effect flag) layered on the sandbox's routable endpoint. The default adapter wraps the claude-agent-sdk streaming-input session. `packages/aci-protocol` and `packages/plugin-format` (the Claude Code plugin shape verbatim) are **frozen interfaces built first** — every lane compiles against them, and the ACI *is* the versioned agent↔control-plane contract that de-risks on-prem upgrades.

## Evidence (live, 2026-07-04)

- The SDK, driven headless (on an OAuth token, no API key), accepted a mid-run message that **steered a tool-using agent at the next loop boundary** (it abandoned a 5-step task to obey the injected instruction). `interrupt()` aborted an in-flight run. Prompt caching worked across turns.
- One-process-per-sandbox sidesteps multi-tenant-in-one-process entirely; the runner prototype (`prototypes/runner/`) ran this shape inside a Sandbox successfully.
- Nuance: steering lands at loop boundaries (tool calls). A single-shot text turn has no intra-turn boundary and completes before a queued message applies — the worker's finish-race logic must account for this.

## Consequences

- `aci-protocol` / `plugin-format` are frozen and versioned from day one, with a compat CI test. This is also mitigation for the top operational risk (the on-prem versioning contract the team has no muscle for yet).
- The adapter boundary keeps the door open for a pi/opencode (air-gapped BYO-model) adapter later at zero cost — not built until a real air-gapped prospect exists.
- The OAuth token proved the mechanism for dev; production runners need a proper API-key / Bedrock / Vertex path (ADR-worthy when that lane lands).
