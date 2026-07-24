# 56. An operator opt-in makes a policy gate grantable; a heuristic never does

Date: 2026-07-17

Status: Accepted

Implements [#558](https://github.com/curie-eng/curie/issues/558).
Supersedes the "a policy gate refuses a grant outright" half of
[ADR-0046](0046-converged-approval-gates-and-durable-provenance.md) Decision C —
and only that half. Every other property ADR-0046 established (durable
runner-authored `gate_kind`/`granted_tool` provenance, loud route resolution,
observe-only resume reconciliation, and the convergence of the two gate paths on
one lifecycle) is unchanged. ADR-0046 itself named this change as the sanctioned
way to reintroduce policy-gate grantability and deferred it here.

## Context

Under ADR-0046 a **policy gate never mints a one-shot tool grant**. The
permission gate stays the sole tool authority, so a policy-gated action costs the
human two approvals: one for the business decision, one for the tool execution.
`translate.py` stamps `approval_gate_kind='policy'` with `approval_granted_tool`
left `None`, and the worker's `approval_grant_tool`
(`apps/worker/src/curie_worker/binding.py`) refused any `gate_kind='policy'`
row outright.

That is correct but not free. The design behind ADR-0046 established why a
*heuristic* cannot close the gap, and this ADR does not relitigate it. The
rejected heuristic let a policy approval mint a grant whenever the model's
manifest-validated `route` resolved to exactly one manifest gate. It fails on
**argument visibility**, an asymmetry between the two approval cards:

- A **permission-gate** card shows the tool **and its rendered arguments**
  (`summarize_tool_call` renders the blocked `tool_input`). The human approves a
  concrete call.
- A **policy-gate** card shows a model-authored framing plus, at most, a bare
  tool name — never rendered tool arguments. If that approval also releases a
  tool grant, the tool then runs on resume with arguments the human never saw.

So policy-grantability is a strict **widening** of the argument-scoping gap
ADR-0035 already accepts, not a preservation of it. A disclosure line does not
fix it: it appears on every granting card, habituates within a week, and needs
the approver to know the business-action → tool mapping to catch a mismatch.
Route selection also picks the **approver audience**, so a single model-chosen
argument would select both the tool and which humans are asked. The bound
degrades linearly as manifests grow. The double-approval UX the heuristic tried
to avoid is not a defect: the two approvals authorize genuinely different things,
and the second card is the argument-visibility control the first cannot deliver.

The residual is real, though: some operators legitimately want a business
approval to also carry the tool through, accept that the card shows no arguments,
and know their own manifest well enough to reason about it. The question #558
answers is *who* may make that trade, and the answer cannot be a heuristic over a
model-chosen route.

## Decision

**Grantability is an explicit, per-gate, operator-authored opt-in in the plugin
manifest. Neither a heuristic nor the model ever confers it.**

### 1. The manifest carries the opt-in

`ApprovalGate` (`packages/plugin-format`) gains an optional boolean
`grantableViaPolicy` (default `false`). An operator marks a gate grantable
explicitly:

```json
{"gate": "close_issue", "route": "deal-desk", "grantableViaPolicy": true}
```

Because a gate's `gate` field **is a tool name** (the runner arms it as a
permission-gated tool and keys its route off it), an opted-in gate declares:
"when a policy approval resolves to route `deal-desk`, also mint a one-shot grant
for the `close_issue` tool." The granted tool is the manifest's `gate` value —
**operator-authored, never a model-supplied string.** The model's `route`
argument only selects among gates the operator already declared; it cannot invent
a tool, and it is validated against the manifest's declared routes exactly as
before.

### 2. One normalization, shared by the validator and the loader

`plugin_format.grantable_routes` (`packages/plugin-format/src/plugin_format/approval_policy.py`)
is the single function that maps the declared gates to `{route: tool}` for
grantable gates, and reports any route claimed by more than one **distinct**
grantable tool as **ambiguous**. Both the deploy-time validator
(`_validate_approval_policy`) and the runtime loader
(`resolve_approval_policy`) call it, so they normalize identically by
construction (`.strip()`, case-sensitive) rather than by two implementations that
must be kept in step. This is the #453/#544 fail-open class stated as a
structural guarantee: a config that validates green cannot arm something
different at runtime, and an executed test pins the agreement.

An **ambiguous** grantable route (two grantable gates, same route, different
tool) is a hard **deploy error** (`approval_policy.grant_route_ambiguous`): left
to runtime it would validate green and arm no grant — the exact silent fail-open
this repo rejects. The loader also excludes an ambiguous route from its grant map
as defense in depth, so even a bundle that somehow bypassed the validator arms no
ambiguous grant.

### 3. The runner stamps, the worker honors

On an accepted policy request the runner stamps
`approval_granted_tool = gate.grantable_tool_for_route(resolved_route)` — the
manifest tool for an opted-in route, or `None` otherwise. `gate_kind` stays
`'policy'`. The worker's `approval_grant_tool` now returns the `granted_tool`
column for a `gate_kind='policy'` row instead of refusing it outright: a
non-opted policy gate leaves the column `NULL` and still grants nothing, so
**ADR-0046's behavior is the exact default for every gate the operator did not
mark grantable.** `granted_tool` is never model-authored, and the human approval,
status, and agent-bind guards are all unchanged, so this does not widen what a
prompt-injected model — or a compromised sandbox, which already authors
`granted_tool` on the permission path — can reach. No new `gate_kind` value and
no migration: the existing nullable `granted_tool` column (migration `0015`) and
its `CHECK (gate_kind IN ('permission','policy'))` are reused as-is.

## Consequences

- **A policy-grant card still shows no tool arguments.** That asymmetry with a
  permission card is unchanged and is the whole reason grantability is an opt-in:
  it converts the residual argument-scoping risk from **ambient** (a heuristic
  granting on every resolvable route) to **deliberate** (an operator marking one
  gate, accepting that its approval carries a tool the human approves without
  seeing its arguments). An operator who does not opt in is exactly where
  ADR-0046 left them.
- **The default is byte-identical to #544.** No `grantableViaPolicy`, or
  `false`, means no grant — same column, same worker verdict, same two-approval
  flow.
- **The worker's "refuse policy outright" defense-in-depth is deliberately
  relaxed for opted-in gates.** It was load-bearing only while policy-never-grants
  was absolute; the opt-in is what the operator trades it for. The permission
  path, the model-forgery protections (`guard_reserved_summary`, the
  manifest-sourced tool), and the NULL-`gate_kind` rolling-deploy fallback are all
  unchanged.
- **The CLI manifest mirrors carry the field** so `curie <tier> approvals` and
  `init --from-spec` round-trip a grantable gate without dropping it; the arming
  and ambiguity logic stays Python-authoritative.
