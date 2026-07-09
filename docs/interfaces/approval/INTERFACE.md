# INTERFACE: Approval / authorizer

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).
> **Kind:** NONE &nbsp;·&nbsp; **Implementations today:** 0 &nbsp;·&nbsp; **Swap-readiness grade:** not separately graded

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

The black line is an **Authorizer** port: a server-side decision, at approval-resolution
time, of whether a given actor is allowed to resolve a given pending approval — plus the
`awaiting-approval` lifecycle state that lets a session durably pause on that decision. What
stays opinionated core is *where* the decision is enforced (server-side, at resolution) and
that gates are policy-triggered, never phase-hardcoded by the platform. What becomes
swappable is the authorizer *implementation* (channel-membership first, then user-group,
explicit user-list, platform-RBAC) behind that one server-side check.

## Current contract

This seam does **not exist in code yet**; the contract below is the *intended* line from
[ADR-0010](../../adr/0010-approval-gates-and-human-in-the-loop.md) and epic
[#22](https://github.com/curie-eng/agentos/issues/22), stated so a future implementation
builds to it. What exists today is only the negative space it must fill:

- **The hardcoded posture the gate replaces.** The runner today builds session options with
  `permission_mode="bypassPermissions"` (`runner/src/agentos_runner/adapter.py:82`) and has
  no tool-permission callback. A permission gate must introduce a `canUseTool` callback that
  replaces this hardcoded bypass, intercepting a model-initiated tool call so it can block
  pending an approval.
- **The frozen status enum the gate extends.** `SessionStatus` (`packages/aci-protocol/src/aci_protocol/events.py:28`)
  today has exactly three values — `DONE = "done"`, `IDLE_AWAITING_INPUT = "idle-awaiting-input"`,
  `CLASSIFIED_FAILURE = "classified-failure"` (lines 35–37). The intended contract adds a
  fourth, `awaiting-approval`, as a backward-compatible frozen-contract change regenerated
  across all three language targets (Pydantic source, JSON Schema, TypeScript, Rust).
- **The durable record + resolve-once semantics.** A durable `Approval` record (Postgres)
  with compare-and-set claim semantics is the intended backing store, so losers of the claim
  race are told it was already resolved. The authorizer check runs server-side at resolution
  time; self-approval is blocked.

## Implementations today

**None.** Zero authorizer implementations, no durable `Approval` record, no `awaiting-approval`
status, and no `canUseTool` gate. The seam lands with epic #22. The suspend/resume path it
relies on (ADR-0003) is built but dormant; an approval is its first intended production use.

## Known leakage

Nothing to leak yet — the file records a placement constraint, not existing code. The single
constraint a future implementation must honor: the authorizer is **enforced server-side at
resolution time**, not inside the sandbox or runner. The runtime `canUseTool` gate blocks the
*tool call*, but the authorization decision (who may resolve a pending approval) must live on
the server that owns the durable `Approval` record, so it cannot be spoofed from inside the
agent's box. Policy gate points ship versioned in the bundle; route bindings (which channel,
who may approve) are per-agent deployment config.

## Cross-links

- **Epic(s):** [#22](https://github.com/curie-eng/agentos/issues/22) — approval gates and human-in-the-loop; adds the durable record, `awaiting-approval` status, `canUseTool` gate, and the authorizer interface.
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — not one of the six graded jobs; a cross-cutting core lifecycle change, not separately graded.
- **ADR(s):** [ADR-0010](../../adr/0010-approval-gates-and-human-in-the-loop.md) — Approval gates and human-in-the-loop (Proposed); grounds this intended line. Composes with [ADR-0003](../../adr/0003-stateless-first-rehydrate-on-resume.md) (stateless-first suspend/resume, the pause mechanism).
