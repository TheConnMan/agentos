# 77. Skill-tier durable approvals stay unavailable, reported not absent

Date: 2026-07-23

Status: Accepted

Implements [#771](https://github.com/curie-eng/agentos/issues/771).
Follows [#766](https://github.com/curie-eng/agentos/issues/766) and
[ADR-0063](0063-message-driven-approval-reply-surface.md); applies the tier
answer-or-report rule of [ADR-0041](0041-every-verb-is-answered-at-every-tier.md).

## Context

`local approvals` and `cluster approvals` gained `--list` and `--resolve --as`
(#506/#736): they read and resolve the durable `Approval` records the API
persists and the worker resumes against. `skill approvals` never grew them; it
takes only gate-config arguments (view/set the bundle's declared gates).

`skill message` talks straight to the local runner's ACI surface, bypassing the
dispatcher, Valkey, worker, and the whole resume path by design — that is the
point of the tier (zero cluster, zero platform, the bundle bytes on disk). So
there is no durable `Approval` record at this tier and no resume machinery: the
#766 keep-alive fix has nothing to keep alive here. Closing the gap means
*deciding what durable approval state means at the skill tier*, which is a design
question, not a parity patch.

## Decision

**Do not implement durable approvals at the skill tier. Report the two verbs as
explicitly unavailable, with a reason and the cross-tier alternative.**

`skill approvals` accepts `--list` and `--resolve` (plus the paired `--as` /
`--reject`) so a `local`/`cluster`-shaped invocation is **declined with a
reason** rather than rejected as an unknown-flag typo — the same accept-to-decline
shape `cluster deploy --secret` uses for its not-yet-delivered capability
(ADR-0009). Using either flag returns an `unsupported` error (exit 4) whose
`{error, fix}` payload names why (no durable store or resume path here) and where
to go instead (`local`/`cluster approvals`, or resolve the gate within the same
`skill message` session). The reason/alt strings are single-sourced in
`cli/src/commands.rs` (`APPROVALS_LIST_REASON`/`APPROVALS_LIST_ALT`), so the help
text and the runtime answer cannot drift (#459). The existing gate-config path
(view/set/clear) is unchanged.

This satisfies ADR-0041's promise — a verb is either implemented or explicitly
reported unavailable at a tier — which today it silently breaks: `skill approvals
--list` currently fails as an unknown flag, indistinguishable from a typo.

## Alternatives considered

- **Implement it via the live runner container** — hold pending approvals in the
  long-lived runner and expose list/resolve over its ACI surface, treating
  "durable" as "as long as the container is up." **Rejected.** It bolts a resume
  path onto a tier #766 deliberately left without one, expands the runner's
  approval lifecycle, and redefines "durable" to something weaker than the
  local/cluster guarantee — a lot of surface and a sacred-`kernel.py`-adjacent
  approval-flow change to make a dev-loop tier imitate a capability it exists to
  do without. The tier's honest shape is: raise a gate, resolve it in-session, or
  move to `local`/`cluster` for durable review. If a concrete need for
  container-lifetime skill approvals appears later, a new ADR supersedes this one.

## Consequences

- The skill tier's approval surface is now honest under ADR-0041: gate config is
  answered; durable list/resolve is reported unavailable with a reason and an
  alternative, not a typo-shaped error.
- No new durable-state or resume code at the skill tier; the runner's approval
  lifecycle is untouched.
- If the local/cluster `approvals` flag surface grows, the skill decline should
  keep accepting the same flags (to decline, not error) — a small maintenance tie
  between the tiers, the deliberate cost of the accept-to-decline shape.
