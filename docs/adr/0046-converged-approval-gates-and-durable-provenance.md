# 46. Converged approval gates: durable provenance, loud route resolution, and observe-only resume reconciliation

Date: 2026-07-16

Status: Accepted

**Superseded in part by [ADR-0056](0056-operator-opt-in-for-policy-gate-grantability.md)**
(back-link added under [ADR-0045](0045-the-status-line-is-the-mutable-part-of-an-immutable-adr.md)):
0056 supersedes the "a policy gate refuses a grant outright" half of Decision C
below — and only that half — making a policy gate grantable by explicit operator
opt-in. Every other property this ADR established (durable
`gate_kind`/`granted_tool` provenance, loud route resolution, observe-only resume
reconciliation, and the convergence of the two gate paths on one lifecycle) is
unchanged.

Implements [#544](https://github.com/curie-eng/curie/issues/544).
Supersedes the provenance discriminator of
[ADR-0035](0035-one-shot-post-approval-allowance.md) (the summary-prefix sniff);
the one-shot grant lifecycle ADR-0035 established is otherwise unchanged.

## Context

ADR-0010 defines one approval primitive with two trigger types, and they were
never actually converged:

- **Permission gate** (#245): the model calls a manifest-`approvalPolicy`-gated
  tool, the runner's `can_use_tool` denies it, and the block records a summary
  carrying the reserved prefix `"Tool call awaiting approval: "` plus the route
  the manifest declares for that tool.
- **Policy gate** (#244): the model itself calls the in-process MCP tool
  `mcp__curie__request_approval(summary, route?)`, whose `route` is optional.

With the same bundle and prompt, the model sometimes did one and sometimes the
other. That model-dependence produced two failures (#544), both of which
**failed open and quiet** on an authority surface:

1. **Authority silently widened.** A policy request with `route=null` had no
   binding to resolve, so the manifest's declared route (approvers + card
   channel) was never consulted and the card fell back to the *requesting*
   channel's membership. The bound approver was refused 403 while anyone in the
   requesting channel would have been accepted.
2. **Approved, then nothing happened.** ADR-0035's one-shot grant is injected
   only when the persisted approval summary starts with the reserved prefix —
   i.e. only for permission gates. A policy-gate approval got no grant, so the
   resume turn either re-called the gated tool (denied again) or, more commonly,
   the model simply never re-called it; either way the approved action never
   ran, with no error and no re-pause record.

The root cause of both is the same: the two paths diverged in *who supplies the
route* and *how grant eligibility is proven*. The permission gate read both from
trusted sources (the manifest, and the tool `can_use_tool` denied); the policy
gate read the route from the model's optional argument and proved grant
eligibility by a string-prefix sniff that a policy gate structurally could not
satisfy.

## Decision

Converge the two paths on one lifecycle by moving both decisions into the
runner (the only component that knows *which tool was denied* and *what the
manifest declares*) and carrying them to the worker as **structured,
runner-authored fields**, and by making every silent widening fail loud.

### 1. Durable provenance replaces the summary-prefix sniff

Two new fields travel on the awaiting-approval `Final`
(`packages/aci-protocol`, a patch bump — both optional with defaults, additive
under ADR-0036):

- `approval_gate_kind`: `'permission'` or `'policy'`.
- `approval_granted_tool`: the tool a resume-boot grant may release, or `None`.

They are persisted as two nullable columns (`gate_kind`, `granted_tool`) on the
`Approval` record (migration `0015`). `apps/worker` `approval_grant_tool` reads
the `granted_tool` column instead of parsing the summary. The permission-gate
path (`runner` `ApprovalGate.block`) sets `pending_gate_kind='permission'` and
`pending_granted_tool` to **the exact tool name `can_use_tool` denied** — a
trusted value, never parsed from a string, never model-supplied.

**A policy gate never mints a grant.** `translate.py` stamps
`approval_gate_kind='policy'` and leaves `approval_granted_tool=None`, and
`approval_grant_tool` refuses a `gate_kind='policy'` row outright. This is the
#430/ADR-0035 invariant restated in durable form: **the model's arguments must
never select which tool receives bypass authority.** The old discriminator let
the model reach it only through a forgeable prefix; the new one removes the
prefix from the grant decision entirely.

The status and agent-bind guards (`status='approved'`,
`row_agent_id == agent_id`) are unchanged and still load-bearing (#430 rebind
leak). `guard_reserved_summary` stays: the `gate_kind IS NULL` rolling-deploy
fallback (below) still trusts the prefix, so removing the guard would reopen
#430 for that window.

### 2. Route resolution moves into the runner and fails loud

`build_approval_server()` becomes per-gate (`build_approval_server(gate)`) so
the `request_approval` tool can see the manifest's declared routes. The tool
validates the model's `route`:

| manifest distinct routes | model's `route` | behavior |
|---|---|---|
| 0 | anything | accept, `route=None` — a generic approval; ADR-0034's channel-membership default is correct for it |
| ≥1 | omitted, exactly 1 declared | bind it |
| ≥1 | omitted, ≥2 declared | `is_error` naming valid routes; **no approval created** |
| ≥1 | present, in manifest | accept |
| ≥1 | present, not in manifest | `is_error` naming valid routes; **no approval created** |

An `is_error` result reaches the model, names the valid routes, and lets it
retry within the same turn — no approval, so nothing widens. Route comparison
normalizes identically to `plugin_format.validate_bundle` / `load_approval_policy`
(`.strip()`), **case-sensitive** (case-folding in the runner alone would miss
the deployment's `approval_routes` binding map). A validator and a runtime
loader that normalize differently turn the gate into a silent fail-open — a
route that validates green arms nothing — so the two are pinned to agree by an
*executed* test that runs a route string through both sides.

The API's `get_approval_route_binding` channel fallback (ADR-0034, for
genuinely agent-less generic approvals) is deliberately **left intact**; the bug
was never the fallback, it was a manifest-gated request arriving with
`route=null`, which is now impossible.

**Server-side widening closed too:** the kernel previously logged a warning and
routed a *named-but-unbound* route's card to the requesting channel (#247). It
now escalates loudly and creates no approval, reversing that #247 decision — the
server-side half of AC2.

### 3. Resume reconciliation ships OBSERVE-ONLY

AC1's loudness comes from two mechanisms already present, both stronger than a
turn-end assertion: a policy request that cannot resolve a route is refused **at
request time** (§2), and the permission-gate **second approval** on resume is
inherently visible and argument-bearing and is what actually completes the
action.

The residual is narrower: a model that, after approval, simply never re-calls
the tool. To make that observable, the worker injects an **authority-free**
marker `CURIE_APPROVAL_RESUMED_KIND=policy` at resume boot (a fact about the
past, granting nothing — contrast `CURIE_APPROVAL_GRANT_TOOL`, which confers
authority). At boot-turn end, if the marker is present, the agent has gates
armed, no permission-gate block occurred, and no side-effecting tool ran, the
runner emits a structured warning frame
(`APPROVAL_NOT_ACTED_CLASSIFICATION`) naming the approval id — **and leaves the
final clean.** No non-clean terminal status.

This is **instrumentation, not proof of AC1**, because its signal
(`side_effect_emitted`) is a proxy for *"some tool ran"*, not *"the approved
action ran"*, sourced from the read-only allowlist in `side_effects.py`. The
gap cuts both ways and both modes are real and accepted:

- **False alarm on the common legitimate path.** A text-only reply is no tool
  call, so `side_effect_emitted` stays False. A text-only business decision is
  the *primary legitimate use* of a policy gate, so a day-one hard-fail would
  flag every gated agent on every such resume.
- **False pass on the actual failure path.** Any incidental non-allowlisted
  tool (a scratch `Write`, any MCP call) flips `side_effect_emitted` True and
  suppresses the warning even though the approved action never ran.

A signal this weak must not gate a terminal status, so the reconciliation ships
observe-only and earns the data for a later enforce decision (#559). Both modes
are pinned by tests as known, accepted behavior.

## Alternatives considered

- **Let a policy gate mint a grant when its route resolves to exactly one
  manifest gate** (the granted tool read from the manifest, never a model
  string), disclosed by a runner-authored `Authorizes tool: <t>` line on the
  card. **Rejected.** A permission-gate card shows the tool *and its rendered
  arguments*; this card would show a model-authored framing plus a bare tool
  name, then run the tool with arguments the human never saw — a strict widening
  of ADR-0035's already-accepted argument-scoping gap, wearing a disclosure's
  clothes. The disclosure habituates (it appears on every granting card), and
  route selection also picks the approver audience, so one model-chosen argument
  selects both the tool and which humans are asked. The double-approval UX this
  avoids is not a defect: the two approvals authorize genuinely different things
  (a business decision vs. a tool execution with visible arguments), and the
  second card is the argument-visibility control ADR-0035 cannot otherwise
  deliver. Operator-gated grantability is the right way to reintroduce this and
  is deferred to #558 (a per-gate `grantableViaPolicy` opt-in), where the
  *operator*, not a heuristic over a model-chosen route, decides.

- **A day-one loud (non-clean) final for the resume reconciliation.** Rejected
  for the reasons in §3: the `side_effect_emitted` signal false-alarms on the
  common path and false-passes on the failure path, so a hard-fail would train
  operators to ignore it — the same alert-fatigue failure that killed the
  disclosure line.

- **Keep the summary-prefix discriminator.** Rejected: it is a string the model
  influences, defended only by `guard_reserved_summary`, and structurally cannot
  express a policy-gate provenance. A durable, server-written column is the
  robust form ADR-0035 itself named as its recommended follow-up.

## Consequences

- Which gate fires is no longer authority-relevant: a policy-gated action costs
  the human two approvals (the business decision, then the argument-bearing tool
  execution), and a permission-gated action is unchanged. That double-approval
  is the deliberate, accepted cost of not letting a model-chosen route confer
  tool authority. #558 makes it optional per gate, operator-controlled.
- **A policy-grant card never shows tool arguments.** This asymmetry is the
  reason grantability must be an explicit operator opt-in (#558) rather than a
  heuristic; recorded here so the supersession chain carries the reasoning.
- **Rolling-deploy window:** the runner image is pinned per sandbox, so a new
  worker can resume a session whose in-flight runner predates this change and
  emits no `gate_kind`/`granted_tool`. For `gate_kind IS NULL` rows only,
  `approval_grant_tool` falls back to the ADR-0035 prefix parse — byte-identical
  to prior behavior, so it neither widens nor regresses. The migration backfills
  existing rows (permission rows from the prefix, policy rows to
  `granted_tool=NULL`, the safe direction); the fallback covers only rows a new
  runner never wrote. It can be removed once no pre-#544 runner can be live.
- **Known instrumentation gap (fail-safe direction):** the resume
  reconciliation warns on some legitimate turns and stays silent on some failed
  ones (§3). It is observe-only precisely so this proxy signal never gates a
  terminal status; promotion to enforce is gated on real false-alarm data
  (#559).
