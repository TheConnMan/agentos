# 10. Approval gates and human-in-the-loop

Date: 2026-07-09
Status: Proposed

## Context

Production business agents pause for a human decision: a discount needs sign-off,
a remediation needs a yes before it runs. Today the platform offers nothing for
this and builders would hand-roll all of it. Concretely, the runner hardcodes
`permission_mode="bypassPermissions"` (`runner/src/agentos_runner/adapter.py`),
there is no tool-permission callback, and the ACI `SessionStatus` has only
`done` / `idle-awaiting-input` / `classified-failure` - no awaiting-approval
state (`packages/aci-protocol`). The suspend/resume path (ADR-0003) is built but
has no production trigger; an approval is exactly the hours-to-days pause it was
designed for.

This is a cross-cutting decision because it changes a frozen contract (the
session status enum), replaces a hardcoded runtime posture (permission bypass),
and defines a durable lifecycle that the worker, runner, and channel all
participate in. It is not a single feature that can be deleted cleanly.

## Decision

Provide **one approval primitive with two trigger types**, policy-driven and
never phase-hardcoded by the platform.

- **Policy gates** (business-level): the agent's own logic (a skill or the
  deterministic engine) decides something needs approval and raises an
  approval request.
- **Permission gates** (tool-level): configuration marks a tool as
  approval-required and the runner intercepts the call via a `canUseTool`
  callback that **replaces the hardcoded bypass**.
- Add an **`awaiting-approval`** value to the ACI `SessionStatus` (a
  backward-compatible frozen-contract change, versioned and regenerated across
  all three language targets).
- Back it with a **durable `Approval` record** (Postgres) using resolve-once
  compare-and-set claim semantics; **suspend** the session on a pending approval
  and **resume** on resolution (the first live use of the dormant suspend/resume
  path).
- **Approval policy** (gate points) ships in the bundle (versioned, evaluable);
  **route bindings** (which channel, who may approve) are per-agent deployment
  config. The authorizer is enforced server-side at resolution time.

## Alternatives considered and rejected

1. **Builders hand-roll approvals in bundle code.** Rejected. Every agent would
   re-implement Slack buttons, pending state, idempotent claim races, and actor
   attribution; the result is error-prone, unauditable, and not portable across
   agents. The platform is supposed to ship this discipline as the default path.
2. **Platform-forced approval phases (a built-in "approve before write" step).**
   Rejected. The platform must never hardcode when an approval fires; that is the
   agent's policy to decide. Gates are policy-triggered, not phase-hardcoded.
3. **Keep `bypassPermissions` and gate only in bundle code or hooks.** Rejected
   for the tool-level case. A permission gate must intercept the runtime tool call
   to reliably block a model-initiated action; an in-bundle check cannot, and it
   loses the durable-pause semantics. (In-bundle hooks remain useful for
   deterministic guardrails, but they are not the approval primitive.)
4. **Hold pending state in memory on the live sandbox.** Rejected. An approval
   that must survive hours-to-days and component restarts cannot live in a pod;
   this forces the durable record plus suspend/resume.
5. **Live-hibernate the sandbox for the duration of the pause.** Rejected.
   ADR-0003 established that sandboxes are stateless: suspend deletes the pod and
   resume rehydrates from history. The pause is a cold rehydrate, not a held
   process.

## Consequences

- First production use of the suspend/resume path; its correctness (duplicate
  side effects at the finish-race, ADR-0003) becomes load-bearing.
- A frozen-contract change (`awaiting-approval`) that ripples through the
  tri-language ACI regeneration and the worker lifecycle.
- Composes with the durable workflow state store: an approval is durable
  cross-turn state.
- If the harness strategy adopts OpenCode, its native allow/ask/deny permission
  gate (where `ask` pauses for human approval) may supersede the `canUseTool`
  implementation; the policy-gate and durable-record halves remain regardless.
  See the harness-strategy ADR.
