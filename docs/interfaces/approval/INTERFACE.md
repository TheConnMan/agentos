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

The durable base of the seam landed with #244; the authorizer port itself is still ahead
(#246). What exists in code now:

- **The durable record + resolve-once semantics (landed, #244).** The `Approval` table
  (`apps/api/src/agentos_api/models.py`) with the resolve-once compare-and-set
  (`crud.claim_approval_resolution`, a conditional `UPDATE ... WHERE status='pending'`) behind
  `POST /approvals/{id}/resolve`; losers of the claim race get 409 naming who resolved it,
  a past-SLA record flips to expired (410). Creation is idempotent on `dedupe_key` (the
  triggering event id).
- **The `awaiting-approval` status (landed, #244).** `SessionStatus.AWAITING_APPROVAL` plus
  the optional `Final.approval_summary` field
  (`packages/aci-protocol/src/aci_protocol/events.py`), regenerated across all three language
  targets as a backward-compatible frozen-contract change (ADR-0010 authorized it).
- **The lifecycle (landed, #244).** A skill raises a policy gate through the runner's
  in-process `mcp__agentos__request_approval` tool (`runner/src/agentos_runner/approval.py`);
  the turn ends `awaiting-approval`, the worker persists the record and suspends the sandbox
  (`kernel._pause_for_approval` — the first live use of the dormant ADR-0003 suspend path);
  resolution enqueues a resume turn onto the ordinary runs stream
  (`apps/api/src/agentos_api/resumequeue.py`), and the kernel's claim path rehydrates the
  thread with its bound boot env (`substrate.resume(env=...)`).
- **The permission gate (landed, #245).** Per-agent config
  (`agents.approval_required_tools`, forwarded as `AGENTOS_APPROVAL_REQUIRED_TOOLS` by the
  worker binding) marks tools approval-required; the runner intercepts those calls
  proactively through an SDK `can_use_tool` callback (`build_can_use_tool`,
  `runner/src/agentos_runner/approval.py`) -- the call is denied before execution, and the
  turn ends `awaiting-approval` on the same override the policy gate uses, so both trigger
  types share one record/suspend/resume lifecycle. An agent with no configured gates keeps
  the historical `bypassPermissions` posture verbatim (zero behavior change). Not yet built:
  a grant mechanism for the resumed turn (an approved tool call is still gated on retry
  after resume; a one-shot allowance delivered at boot composes with #247's policy
  evaluation).

## Implementations today

**Zero authorizer implementations** — resolution is currently authorized by the shared API
key alone, like every other endpoint; WHO may resolve (channel membership first, then
user-group, explicit user-list, platform-RBAC) and the self-approval block land with #246 at
the resolve endpoint, which is deliberately where the decision point already sits. The
durable record, the `awaiting-approval` status, the request tool, and the suspend/resume
lifecycle are live (#244).

## Known leakage

The placement constraint held in the landed base and must keep holding: the authorizer is
**enforced server-side at resolution time**, not inside the sandbox or runner. The runner
only *raises* a request (its tool marks the turn; the record, the resolve CAS, and the
resume enqueue all live with the API/worker), so a compromised sandbox cannot mint or
resolve an approval. The runtime `canUseTool` gate (#245) will block the *tool call*, but
the authorization decision (who may resolve a pending approval) stays on the server that
owns the durable `Approval` record. Policy gate points ship versioned in the bundle; route
bindings (which channel, who may approve) are per-agent deployment config (#247).

## Cross-links

- **Epic(s):** [#22](https://github.com/curie-eng/agentos/issues/22) — approval gates and human-in-the-loop; adds the durable record, `awaiting-approval` status, `canUseTool` gate, and the authorizer interface.
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — not one of the six graded jobs; a cross-cutting core lifecycle change, not separately graded.
- **ADR(s):** [ADR-0010](../../adr/0010-approval-gates-and-human-in-the-loop.md) — Approval gates and human-in-the-loop (Proposed); grounds this intended line. Composes with [ADR-0003](../../adr/0003-stateless-first-rehydrate-on-resume.md) (stateless-first suspend/resume, the pause mechanism).
