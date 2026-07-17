# 50. A declared approval policy is armed exactly as declared, or the runner refuses to boot

Date: 2026-07-17

Status: Accepted

Implements [#520](https://github.com/curie-eng/agentos/issues/520) (sub-items 1
and 2). Ports two fail-closed patterns reviewed in `xai-org/grok-build`; the
patterns only, never the mechanism (Grok's sandbox is weaker than ours on every
axis — in-process Landlock/Seatbelt, Linux-only, no egress allowlist, no
per-agent secrets).

## Context

The runner decides which tools are approval-required by unioning two sources
(`__main__`): the operator's `AGENTOS_APPROVAL_REQUIRED_TOOLS` (a bare list of
names, no routes) and the bundle manifest's `approvalPolicy` gates (versioned
with the agent, each carrying its route). Two fail-open surfaces sat in that
merge.

**1. Enforcement intent was read from the resolved map, not the declaration.**
`load_approval_policy` swallowed `json.JSONDecodeError`, `ValueError`, and
`OSError` and returned `{}`, on the reasoning that it mirrored the
hooks/`systemPrompt` readers and that "the authoritative parse gate stays
`load_plugins`". But `{}` is not a neutral value here: `__main__` builds
`approval_gate = ... if gated_tools else None`, and a `None` gate means
`can_use_tool` is never wired at all — the hardcoded bypass posture returns. So
a bundle that *declared* gates but failed to parse booted with **nothing gated,
silently**. The empty map is indistinguishable from a genuine no-policy bundle.

The `load_plugins` backstop is real but incidental, and it does not cover the
fake tier: `__main__.factory` returns `FakeModelSession` **before**
`load_plugins` is ever called. On `skill`/`local --fake-model` — the tier
operators use to rehearse approvals — the swallow was the whole gate. This repo
already treats fake-tier divergence from the real gate path as a genuine bug,
not a testing artifact (`fake.py`, citing #561/#544).

The distinction the old docstring missed: hooks and `systemPrompt` degrade to a
**narrower capability set** and `load_plugins` is a sufficient backstop for
them. `approvalPolicy` degrades to a **wider authority set**. Same swallow,
opposite blast radius.

**2. A bundle could redefine the route of an operator-set gate.** The gated-tool
set is a union, so a bundle already could not *remove* an operator's gate — the
append-only half was correct. The routes were not. `route_by_tool` was assigned
verbatim from the bundle's map, and the operator's list carries no routes of its
own, so a bundle gate naming a tool the operator independently gated silently
chose **which of the operator's own approval channels governs the operator's own
gate**. The trusted name survives; its authority is redirected. ADR-0046 closed
the adjacent fail-open (a named-but-*unbound* route no longer falls back to the
requesting channel) but never addressed a bundle naming a route the operator
*has* bound.

Note what is **not** in scope. The egress allowlist was the issue's suspected
home for pattern 1 and turns out to be already fail-closed end to end:
`resolve_provider_egress_cidrs` hard-errors on an empty, failed, or
non-globally-routable resolution rather than returning an empty vec, the chart's
default-deny egress policy renders unconditionally, and `any_egress` is already
derived from the declared opts (ADR-0032). No change was warranted there.

## Decision

**A declared approval policy is armed exactly as declared, or the runner refuses
to boot.** Both halves fail closed, before the first turn.

### 1. Enforcement intent is read from the raw declaration

`load_approval_policy` reads the manifest's raw JSON *first* and answers "is a
policy declared?" from it, never from the parsed result. Once an
`approvalPolicy` key is present, enforcement intent is established and no
downstream failure can revoke it — a parse error raises `ApprovalPolicyError`
instead of returning `{}`. The empty map is reserved for the honest cases: no
plugin dir, no manifest, no `approvalPolicy`, or an explicitly empty `gates`
list.

An unreadable manifest also raises: a manifest that will not parse cannot prove
it declares no policy, so the fail-closed reading is to refuse rather than
assume.

Every **distinct declared gate name** must end up armed. The comparison is
against distinct names rather than a count, deliberately: two entries for one
tool are a last-wins duplicate that `plugin_format.validate_bundle` accepts, and
rejecting them here would crash-loop a deploy-valid bundle. A validator and a
loader that disagree are the #453 fail-open shape in reverse.

### 2. A bundle may add gated names, never redefine an operator-set one

`build_approval_gate` (extracted from `__main__`, so the merge is testable
rather than inline) raises when a bundle gate names a tool already in the
operator's list.

It **raises rather than resolving to a side, because both resolutions widen**:
honouring the bundle's route lets an untrusted bundle pick the approving
audience, and dropping it falls back to ADR-0034 channel membership, which may
be wider than the route the operator would have chosen. Refusing is the only
reading that neither widens nor lets the bundle redefine. This mirrors
ADR-0046's "escalate loudly, create no approval" over a silent fallback.

### 3. The CLI mirror follows

`parse_manifest_gates` mirrored the old two-tier semantics (missing required key
→ whole-manifest reject; present-but-empty → drop that gate, siblings survive).
The tiers are now collapsed: any gate the runner cannot arm refuses the whole
manifest, so the CLI reports a usage error for both shapes. Left as-is, `skill
approvals` would report `Bash` as armed for a manifest the runner refuses to
boot on — a reporting/runtime drift in the same family as #607.

## Alternatives considered

- **Keep `{}` and rely on `load_plugins`.** Rejected: it does not run on the
  fake tier at all, and it makes an authority boundary depend on a *different*
  function happening to fail first. Enforcement intent must be intrinsic to the
  declared policy — the substance of the ported pattern.
- **Arm the gates that did parse and drop the rest.** Rejected: a partially
  armed policy is the most dangerous outcome. The operator reads "gated" and
  gets a subset, with no signal which gates are missing.
- **Let the bundle's route win for an operator-set tool** (bundle is more
  specific). Rejected: it is exactly the hollow-out — the operator's `Bash` gate
  keeps its trusted name while an untrusted bundle picks its audience.
- **Silently ignore the bundle's route for an operator-set tool.** Rejected: it
  looks safe but falls back to ADR-0034 channel membership, which can be *wider*
  than the bundle route it replaced. It trades a visible conflict for a quiet
  widening.
- **Pin the egress/secret profile across suspend/resume** (#520 sub-item 3).
  Deferred, not rejected — see below.

## Consequences

- **Behavior change operators must know about:** a bundle whose `approvalPolicy`
  is malformed, partially unarmable, or conflicts with the operator's gated-tool
  list now fails the runner at boot instead of running ungated. On the real path
  `load_plugins` already rejected most of these, so the visible change is
  concentrated on the fake tier and on the new operator/bundle conflict. A
  deploy-time-valid bundle is unaffected: `validate_bundle` rejects every shape
  this now raises on, except the operator-conflict case, which is deployment
  config the validator cannot see.
- **The fake tier gains the gate it was missing**, so approvals rehearsed on
  `skill`/`local --fake-model` now match the deployed tier's arming behavior.
- **The operator/bundle conflict is a boot failure, not a warning.** An operator
  migrating a tool from the `AGENTOS_APPROVAL_REQUIRED_TOOLS` stopgap to a
  versioned manifest gate must drop it from the operator list in the same
  change, rather than running with both. The error names both sides and the two
  ways out.
- **Sub-item 3 (resume pinning) remains open.** A resumed turn re-derives its
  whole execution profile — model, bundle ref, connector secrets, gated-tool
  list — from live config at resume time (`BindingResolver.resolve` /
  `boot_env`), so config widened while an approval pends is picked up by the
  resumed turn. That mirrors fresh-claim semantics by design. Pinning it needs
  the profile persisted at suspend and a migration to carry it, which is its own
  reviewed change. Note the issue's stated motivation for it — that the ADR-0035
  grant discriminator is model-forgeable — no longer holds: ADR-0046 replaced the
  summary-prefix sniff with runner-authored `gate_kind`/`granted_tool` columns,
  and the grant itself is server-derived, agent-bound, tool-scoped and
  single-use. The remaining surface is the surrounding profile, not the grant.
