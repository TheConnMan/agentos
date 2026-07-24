# 36. ACI semver, reader-policy asymmetry, and a wire-lock gate that enforces it

Date: 2026-07-16

Status: Accepted

Implements [#455](https://github.com/curie-eng/curie/issues/455).

## Context

The ACI (`packages/aci-protocol`) is the strongest seam in the system: every
lane (runner, worker, CLI, UI) compiles against it in three languages, and it is
the wire a second harness must satisfy. ADR-0005 declared it a **frozen,
versioned contract with a compat CI test** and stopped there. In practice
"frozen" was neither expressed nor enforced:

- `PROTOCOL_VERSION` stayed `0.1.0` while four wire-visible changes shipped with
  no bump: `SessionStatus.AWAITING_APPROVAL` + `Final.approval_summary`,
  `Final.approval_route`, the `QueuedTurn`/`ReplyHandle` promotion (#7), and
  `ReplyHandle.endpoint` (#19).
- Every model was `extra="forbid"` and the decoder gated on exact string
  equality (`version != PROTOCOL_VERSION`), with `ProtocolVersionLiteral`
  pinning the version as a schema `const`. Under that reader policy a new
  optional field breaks an old consumer exactly as hard as a rename, so there is
  **no such thing as a compatible minor** -- semver on top of strict readers is
  decoration.
- The one gate, `test_schema_compat.py`, asserts `render_schema() == committed`.
  Because a model change is regenerated and committed together, it always goes
  green. It pins artifact *sync*; it never pinned *compatibility*.
- `AGENTS.md` told every agent the schema-compat test "fails any
  non-backwards-compatible change." That was false, and a rule that promises a
  protection which does not exist is why four changes walked through without
  anyone reaching for the version.

This ADR **amends the frozen-ACI posture of ADR-0005**. ADR-0005 remains the
decision to make the ACI a frozen, versioned contract; it is not superseded.
What changes is *how* frozen is expressed and enforced: the reader policy, the
version's type, the compatibility rule, and the gate. The four un-bumped changes
were breaking under the readers in force at the time, so the honest number is a
minor bump to `0.2.0`, not a retro-fitted compatibility claim.

## Decision

**1. Semver, independent of the Curie release, with a written change-class
table.** The protocol version tracks the wire contract's compatibility, not the
product's cadence. The bump class for each change:

| Change class | 0.x today | Post-1.0 |
|---|---|---|
| New optional field (consumer ignores it) | patch | minor |
| New required field | minor (breaking) | major |
| New enum value | minor (breaking) | major |
| Field removal | minor (breaking) | major |
| Field rename | minor (breaking) | major |
| Type change | minor (breaking) | major |
| Doc/description only (no wire change) | none | none |

Compatibility rule a consumer applies: accept a wire version with the same
`major.minor` under 0.x (the same `major` after 1.0); reject otherwise, with a
message naming both versions. Under 0.x only a **new optional field is
compatible** (patch): tolerant consumers ignore it. Every other class breaks an
old consumer, so it bumps the minor (the breaking axis before 1.0). A
same-major-minor **patch** difference is explicitly *not* an error.

**2. Strict producers, tolerant consumers -- the classic asymmetry.**
Constructing an event with an unknown field stays an error (producer mistakes
caught at the source). A decoder reading the wire **ignores fields it does not
model**, so a new optional field is genuinely backward compatible and a minor
bump means something. The version gate becomes compatibility-aware
(`major.minor` match under 0.x), not exact-match; this requires the `version`
field to stop being a `Literal` const, since a const cannot express a range.

**3. Unknown enum values REJECT; they do not degrade.** `SessionStatus` is
**control-bearing, not informational**: `awaiting-approval` drives suspend-and-wait
(ADR-0010), `done` drives finalize, `classified-failure` drives escalate. A
consumer that degraded an unknown status to a fallback would take a **wrong
control action silently** -- degrading a future approval-like status to `done`
would finalize a turn that is actually pending a human decision, dropping the
approval on the floor. That is strictly worse than a loud failure. The rule
composes with the version policy: a new enum value is classified breaking, so it
bumps the minor, so an old consumer rejects the payload at the version gate
(with both versions named) before it ever meets the unknown token. The asymmetry
with unknown *fields* is principled: an unknown field is information the consumer
was never using; an unknown enum value is a control instruction it cannot follow.

**4. A wire-lock gate that normalizes the version out.** A committed
`schema/wire.lock` pins `{protocol_version, wire_sha256}`. The fingerprint is
taken over the built schema **with the version normalized out** (drop `title`
and `protocolVersion`, replace any residual version-const value with a fixed
placeholder), so the hash reflects the wire shape alone. Gate logic: hash
unchanged -> pass (a gratuitous version-only bump is legal); hash changed and
version unchanged -> **fail** with a message naming the semver table and the
bump to make; hash changed and version changed -> pass, and the lock is
regenerated. The regenerator refuses to write a lock whose hash changed while
the version did not, closing the escape hatch of regenerating the lock without
bumping. Without version normalization every bump would change the hash and the
gate would degenerate into the same regenerate-and-diff tautology it replaces.

## Alternatives considered

- **Prune unknown keys on read (keep `extra="forbid"`, strip extras before
  validating).** Rejected. It would tolerate unknown fields at the reader, but
  it does **not** change the emitted JSON Schema -- the committed schema would
  keep advertising `additionalProperties: false` while the real reader tolerated
  extras. That re-creates exactly the "the rule promises a protection that does
  not exist" defect this issue exists to kill: the committed schema would be
  lying. The chosen approach (tolerant config plus a reader-context validator)
  makes the schema tell the truth as a byproduct, and pydantic propagates the
  reader context into nested models for free, where pruning would need
  hand-written recursion per nesting site.
- **Degrade unknown enum values to a fallback.** Rejected for the fail-closed
  reasoning in decision 3: a silent wrong control action on the approval path is
  worse than a loud reject.
- **Hash the raw schema without normalizing the version.** Rejected: the version
  is embedded in the schema in three places, so every bump would move the hash
  and the gate could never distinguish "the wire changed" from "someone bumped."

## Consequences

- `additionalProperties: false` disappears from the generated JSON Schema and
  `deny_unknown_fields` from the generated Rust (both were byproducts of the
  strict read path, which is the path being loosened); TypeScript interfaces gain
  an index signature and become tolerant. Producers stay strict by construction.
- `PROTOCOL_VERSION` is `0.2.0`, with every committed artifact regenerated in the
  same change.
- Published images lagging `main` is a normal condition here. Post-change, a
  `0.2.x` consumer meeting a `0.1.0` producer rejects loudly and correctly with
  both versions named -- the intended outcome, not a defect. There is no in-band
  version negotiation and none is planned.
- **Security note (narrow):** the tolerant reader sits on the runner->worker
  boundary that carries approval control state. Dropping an unmodelled field is
  safe today, but the "new optional field -> patch" classification must be
  applied with care to any future field that carries authorization scope -- such
  a field being silently ignored by an old consumer is a compatibility question
  with a security edge. The enum-reject rule (decision 3) is the fail-closed
  backstop on that same path.
- **Out of scope, tracked separately (per #455):** the unfrozen env contract
  that shadows `SessionConfig` (the approval/history vars the worker injects and
  depends on), and conformance's failure to exercise the approval path. Both are
  real gaps in what "frozen" covers.
