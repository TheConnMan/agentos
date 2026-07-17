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

**2. The anti-hollow-out property was already held — by the union.** The gated
tool set is `operator | bundle`, and because no bundle-supplied value is ever
subtracted from it, a bundle cannot keep a trusted name while emptying what it
restricts. That is the Grok pattern's core, and AgentOS already satisfies it.
What it did not hold was a regression *guard*: nothing pinned the union, so a
later refactor to a dict update, a bundle-wins precedence, or `policy_routes`
used directly as `required` would have broken it silently.

The residual the union does not close is which **route** governs a tool both
sources name. `route_by_tool` is taken verbatim from the bundle, and the
operator's list carries no routes, so the bundle's route rides an operator-gated
name and picks the approving audience. It is bounded: the bundle can only name a
route the operator has itself bound to a channel in `approval_routes`, and
ADR-0046 refuses an unbound one outright. The tool stays gated; the audience
stays drawn from channels the operator chose.

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

### 2. The union is the anti-hollow-out mechanism, and is now pinned as one

`build_approval_gate` (extracted from `__main__`, so the merge is testable
rather than inline) keeps the union and documents it as load-bearing rather than
incidental. An adversarial test pins it: a bundle that re-declares the
operator's own gate and adds its own must not drop either operator name. Any
rebuild of this merge that lets bundle config subtract fails that test.

An overlapping name **warns and proceeds**; it does not refuse. We first
implemented the refusal and reversed it on review, because the refusal is
disproportionate to a bounded widening and the crash it causes is reachable
through the product's own documented flow:

- `agentos <tier> approvals` reports the operator field and the deployed
  manifest's gates as **one unlabeled list** (`cli/src/commands.rs`, the #607
  union), while `--gate` writes a **full replacement** through `PATCH
  /agents/{id}`. So an operator who reads the displayed gates and re-passes them
  with one more writes bundle-declared names into the operator field. That is
  the obvious flow, and it would have armed a boot-fatal error.
- Nothing catches the overlap earlier: the API validates only non-empty and
  comma-free, and `validate_bundle` cannot see deployment config.
- Resume re-derives the same config, so an operator PATCHing a gate while an
  approval pends would turn the approved action's resume into a crash — the
  human says yes and the action never runs.

Refusing to boot over a widening this bounded, in exchange for that, is a worse
failure than the one it prevents. The honest fix is to close the CLI's
read/write asymmetry (label provenance, make `--gate` additive) and only then
consider a config-time refusal; that is its own change, tracked as follow-up.

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
- **Refuse to boot on an operator/bundle overlap.** Implemented, then reversed
  on review — see Decision §2. It makes a bounded widening fatal, and the
  `approvals --gate` replace-semantics walk operators straight into it.
- **Silently ignore the bundle's route for an operator-set tool.** Rejected: it
  looks safe but falls back to ADR-0034 channel membership, which can be *wider*
  than the bundle route it replaced. It trades a visible conflict for a quiet
  widening, and unlike the warn-and-proceed form it also discards information
  the operator's own binding map authorized.
- **Reject the overlap at `PATCH /agents/{id}` instead of at boot.** The right
  end state — a config-time refusal is the same verdict without the crash-loop —
  but it needs the in-force manifest resolvable at PATCH time and the CLI's
  read/write asymmetry closed first, or it just moves the footgun. Follow-up.
- **Pin the egress/secret profile across suspend/resume** (#520 sub-item 3).
  Deferred, not rejected — see below.

## Consequences

- **Behavior change operators must know about:** a bundle whose `approvalPolicy`
  is malformed or partially unarmable now fails the runner at boot instead of
  running ungated. A deploy-time-valid bundle is unaffected — `validate_bundle`
  rejects every shape this raises on — so on the real path `load_plugins`
  already rejected these and the visible change is concentrated on the fake
  tier. The failure is loud: a structured `error_class=` line then a non-zero
  exit before the port binds, matching the module's credential and session-start
  precedents.
- **The fake tier gains the gate it was missing**, so approvals rehearsed on
  `skill`/`local --fake-model` now match the deployed tier's arming behavior.
- **Boot logs now carry the offending manifest field's value.** The pydantic
  validation error is interpolated into the raise, so a malformed
  `approvalPolicy` echoes the failing field into pod logs where it was
  previously swallowed. Bounded to the single failing field, and the manifest
  carries secret *names* only (never values), so this is a diagnosability win
  rather than a leak — but it is a change in what boot logs contain.
- **An operator/bundle overlap warns and proceeds.** The tool stays gated; the
  bundle's route decides the audience, bounded by the operator's own
  `approval_routes` binding map. Closing that residual properly starts with the
  CLI read/write asymmetry (Decision §2), not with a runner-side refusal.
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
