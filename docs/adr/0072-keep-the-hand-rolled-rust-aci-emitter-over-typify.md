# 72. Keep the hand-rolled Rust ACI emitter; typify cannot express the wire contract's runtime semantics

Date: 2026-07-22

Status: Accepted

Closes the open refactor named in
[ADR-0017](0017-tri-language-contract-codegen.md) ("the emitter implementation is
free to change, see #81") by recording the evaluation and its outcome. ADR-0017
stays the decision that Pydantic is the source of truth and the regenerate-and-diff
gate is the enforcement; this ADR only settles the emitter question it left open,
and it changes neither of those. Read
[ADR-0036](0036-aci-semver-and-reader-policy.md) for the reader policy this ADR
leans on: the version gate discussed below is the Rust lane's runtime enforcement
of 0036's compatibility rule.

## Context

The Rust ACI types (`packages/aci-protocol/generated/rust/src/lib.rs`) are
produced by a hand-rolled emitter, `aci_protocol.rust_export`, which introspects
the same Pydantic models the JSON Schema is built from and writes idiomatic serde
structs and internally-tagged enums. The TypeScript lane, by contrast, is
generated from the committed JSON Schema by `json-schema-to-typescript`.
[#81](https://github.com/curie-eng/curie/issues/81) asks whether the Rust lane
should switch to the same posture: generate from the JSON Schema with
[`typify`](https://github.com/oxidecomputer/typify), the Rust crate that emits
types from JSON Schema, and retire the bespoke emitter.

The appeal is real. A schema-driven generator is a maintained third-party
toolchain rather than 411 lines of our own AST-walking, it would make the two
generated lanes symmetric, and "generate from the one committed schema" is a
cleaner story than "Rust introspects the models directly while everyone else
reads the schema."

The question is whether typify would faithfully reproduce the current types.
"Faithfully" here is not cosmetic: the generated Rust is a **runtime decoder** on
the wire, not just a set of type aliases. It enforces three things that are
load-bearing:

1. The outbound/inbound frames are internally-tagged discriminated unions
   (`#[serde(tag = "type")]` / `#[serde(tag = "kind")]`), so a frame is routed to
   its variant by its discriminant, not guessed by shape.
2. A custom `require_compatible_protocol_version` deserializer rejects a wire
   `version` outside the build's `major.minor` compatibility window and names both
   versions. This is ADR-0036's reader policy, executed on the Rust lane.
3. Shared transport literals and boot-env key constants
   (`PROTOCOL_VERSION`, `RUNS_STREAM_DEFAULT`, the `env_keys` module, and so on)
   are emitted from Python constants so the CLI does not retype them and cannot
   drift from the source of truth.

None of the three is a shape the JSON Schema carries. The schema describes the
data at rest; these are decode-time behaviors and out-of-band constants.

## Decision

**Keep the hand-rolled emitter. Do not adopt typify.** The evaluation below ran
typify against the committed schema and compiled the result; the output regresses
correctness, drops the version gate, and still requires hand-written supplements
for everything the schema does not carry. A hybrid saves nothing, because the
parts typify cannot do are exactly the parts worth keeping.

### The evaluation (what typify actually produced)

`cargo typify` (v0.4.2) was run against the committed
`packages/aci-protocol/schema/aci-protocol.schema.json`, and the output was
compiled and exercised with round-trip tests. Findings, in descending order of
severity:

**1. The discriminated unions become `#[serde(untagged)]`, which silently
misroutes overlapping variants.** typify does not translate the schema's
`oneOf` + `discriminator` into an internally-tagged serde enum. It emits:

```rust
#[serde(untagged)]
pub enum OutboundEvent { TextDelta(TextDelta), ToolNote(ToolNote), Final(Final), ... }
```

and it types each variant's discriminant as a plain `String` (`pub type_:
String`), discarding the `const` constraint. An untagged enum decodes by trying
each variant in declaration order and taking the first that structurally fits.
`TextDelta` (`type`, `text`, `version`) is a structural subset of both `ToolNote`
and `Final`, and none of the structs deny unknown fields, so a `tool_note` or a
`final` frame deserializes as `TextDelta`. This is not a hypothetical. Compiled
and run:

```rust
let raw = r#"{"type":"tool_note","text":"running Bash","tool":"Bash","version":"0.2.3"}"#;
let decoded: OutboundEvent = serde_json::from_str(raw).unwrap();
assert!(matches!(decoded, OutboundEvent::TextDelta(_))); // passes
```

The frame the runner meant as a tool note is read by the consumer as assistant
text. The hand-rolled emitter cannot produce this class of bug: an
internally-tagged enum reads the `type` field and routes on it, and an unknown
discriminant is a decode error rather than a wrong-variant success. typify has no
option to force internal tagging from a discriminator; the fix would be to
post-process every union out of typify's output, which is a rewrite as large as
the emitter it would replace.

**2. The protocol-version compatibility gate is gone.** typify renders the
semver-patterned `version` field as a newtype whose deserializer checks the
regex and nothing else:

```rust
let raw = r#"{"type":"final","version":"9.9.9","text":"x","status":"done"}"#;
assert!(serde_json::from_str::<OutboundEvent>(raw).is_ok()); // passes
```

`9.9.9` is well-formed semver, so typify accepts it. The hand-rolled emitter's
`require_compatible_protocol_version` rejects it, because `9.9.9` is outside the
build's `major.minor` window. That rejection is ADR-0036's reader policy on the
Rust lane, and the compat rule (accept same `major.minor` under 0.x, name both
versions on rejection) is a comparison against the build's `PROTOCOL_VERSION`
constant. It is not expressible in JSON Schema at all, so no schema-driven
generator can emit it. This would have to be hand-written and threaded into the
generated decoder regardless of which generator runs.

**3. The out-of-band constants disappear.** `PROTOCOL_VERSION`, the transport
literals (`RUNS_STREAM_DEFAULT`, `WORKER_GROUP_DEFAULT`, and the rest), and the
`env_keys` module of boot-env variable names are all absent from typify's output,
because they are not in the JSON Schema. They live in Python constants and
`BootEnv.env_keys()`, and the emitter writes them into the crate so the CLI pins
against them instead of retyping the literals. A typify lane would need a second
hand-written generation step to supply them, which is most of what
`rust_export.py` already is.

**4. Two new dependencies and a 10x size increase, for a worse artifact.** typify
maps `format: uuid` to `uuid::Uuid` and the semver pattern to a `regress`-backed
newtype, pulling `uuid` and `regress` into a crate whose current dependencies are
exactly `serde` + `serde_json`. The generated file grows from 407 lines to 4,405
(builder structs, `From`/`TryFrom`/`FromStr`/`Display` impls, and the full JSON
Schema re-embedded as doc comments on every type). The emitter deliberately maps
`uuid.UUID` to `String` (documented in `rust_export.py`) precisely to keep the
Rust lane dependency-free; typify reverses that call.

### Why the two lanes are asymmetric on purpose

The obvious objection is that TypeScript is already generated from the schema, so
Rust "should" be too. The asymmetry is correct. TypeScript types are erased at
runtime, so the TS lane never enforces the discriminant or the version window
anyway; `json-schema-to-typescript` emitting shapes is all the TS lane was ever
going to do. The Rust lane, through serde, is a live decoder that executes the
reader policy. The schema is a faithful description of the data; it is not a
description of the decode-time behavior, and only the lane that decodes at runtime
needs that behavior. Generating the runtime lane from a data-only description is
what loses findings 1 and 2.

## Alternatives considered

- **Adopt typify wholesale.** Rejected on findings 1-4: it introduces a
  variant-misrouting decode bug, drops the version gate that enforces ADR-0036 on
  the Rust lane, and still cannot emit the out-of-band constants, so it does not
  even fully replace the emitter it removes.
- **Hybrid: typify for the structs, hand-written code for the unions, version
  gate, and constants.** Rejected. The hard, valuable parts of the emitter are
  exactly the parts typify cannot do. Keeping typify for the plain structs while
  hand-maintaining the tagged unions, the compat deserializer, and the constant
  modules means owning both a typify configuration/post-processor and the
  bespoke code, which is strictly more surface than today for no reduction in the
  hand-written core. The struct emission is also the least error-prone part of
  `rust_export.py`; it is not where the maintenance cost lives.
- **Configure typify to close the gaps** (map uuid to String, force tagging).
  Partially possible and partially not. The uuid mapping is a config knob; the
  internal tagging is not, because typify has no discriminator-to-internal-tag
  path. The version gate and the constants are outside any generator's reach by
  construction. The configurable subset does not include either blocker.
- **Add `additionalProperties: false` / `deny_unknown_fields` to tighten typify's
  structs.** Rejected and orthogonal. The reader path is deliberately tolerant of
  unknown fields (strict producers, tolerant consumers, per the packages/ rule),
  so denying unknown fields would break the compatibility posture, not fix the
  untagged misrouting, which is a variant-selection problem rather than an
  extra-field problem.

## Consequences

- `aci_protocol.rust_export` remains the emitter, and this ADR-0017 open item is
  closed as "evaluated, keep hand-rolled" rather than left dangling. #81 can be
  closed against this record.
- The emitter stays free to evolve under the ADR-0017 constraint (Pydantic
  authoritative, regenerate-and-diff gate enforcing), including borrowing narrow
  ideas from what typify does well (for example, richer derive sets on generated
  types) as long as the tagged unions, the version gate, and the constants stay.
- The two generated lanes stay asymmetric by design: TypeScript from the schema,
  Rust from the models. The asymmetry is a recorded decision now, not an
  accident, so a future reader does not re-open it as an inconsistency to
  "clean up."
- If a JSON-Schema-to-Rust toolchain later gains internal-tagging-from-discriminator
  and a hook for custom field deserializers, the two blockers that are inherent to
  the schema (the version gate and the constants) still remain, so a re-evaluation
  would start from "does the maintained toolchain plus the unavoidable
  hand-written supplements beat the current single emitter," not from scratch.
