# 74. Versioned JSON Schemas for every agent-facing CLI result

Date: 2026-07-22

Status: Accepted

Extends ADR-0021 (the `curie` CLI is a harness whose primary consumer is a
coding agent, so its `--json` output is a machine contract) and sits alongside
the ADR-0021 schema work that first published the `error`, `eval`, `message`,
`observability`, and `status` schemas. Supersedes nothing.

Closes [#634](https://github.com/curie-eng/curie/issues/634) ("publish a
committed, versioned JSON Schema for every agent-facing CLI result").

## Context

The CLI emits a machine-readable JSON object on stdout under `--json` for every
agent-facing verb (ADR-0021, issue #456). An agent consuming that output needs to
(a) validate a payload it received, and (b) detect when a payload's shape changes
in a way that breaks its parsing.

Only five result families had a committed schema (`cli/schema/{error,eval,message,
observability,status}.schema.json`). Every other family — the lifecycle result
verbs (`kill`, `resume`, `budget`, `reset-thread`, `delete`), the read verbs
(`versions`, `memory`, `approvals`), `init`, `deploy`, `check`, `guide`,
`secrets`, the operator verbs (`local`/`cluster` `up`/`down`/`status`/`rebuild`/
`comms`), the model sweep, and the uniform `--dry-run` plan — was pinned only by
Rust assertions inside the CLI's own test suite. A consumer could not validate
those payloads and had no artifact against which to detect a breaking change.

There was also no stated rule for what counts as a compatible change, so nothing
distinguished "a field was added" (safe for a tolerant reader) from "a field was
removed or retyped" (breaks readers) — and nothing forced the second kind to be
visible.

## Decision

1. **Every agent-facing `--json` result maps to a committed JSON Schema with an
   explicit version identity.** Schemas live in `cli/schema/*.schema.json`. Each
   carries a versioned `$id` whose last path segment is `vN` (e.g.
   `https://schemas.curie.dev/cli/kill/v1.json`); that `N` is the schema's
   version identity. Several results may map to one schema (all `message`
   variants share `message.schema.json`; every `--dry-run` plan is the
   `dry-run.schema.json` shape, embedded as one branch of each family's schema).

2. **`cli/schema/index.json` is the inventory** — the single source of truth
   mapping each result family (by its `CliOutput` type name, or by a
   hand-declared free-function/`emit_json` builder name) to its schema file and
   version.

3. **A contract test enforces the inventory (anti-drift).**
   `cli/tests/schema_inventory.rs` walks `cli/src` for every `impl CliOutput`
   (a syntactic property `syn` enumerates exhaustively, the same technique the
   #691 field-parity and #699 emit-parity gates use) and fails if any is absent
   from `index.json`, if a declared schema file is missing or does not compile,
   or if an entry's `version` disagrees with its schema's `$id`. A new result
   family that lands without a schema fails CI. `cli/tests/json_contract.rs`
   additionally validates the real `to_json` output of every family against its
   schema. Results emitted directly through `Ui::emit_json` rather than a
   `CliOutput` are the narrower half (a schema cannot be attributed to them
   syntactically); they are hand-declared and their `.emit_json(` call sites are
   pinned by a per-file count in `index.json`, so a new one trips the gate until
   declared — the same honest scope limit ADR-0021's emit-parity gate documents.

4. **Schemas ship with the published CLI artifact.** `build.rs` embeds every
   `cli/schema/*.schema.json` into the binary. `curie schema-index` prints the
   inventory index; `curie schema-index <name>` prints a named schema. Both
   work from a released binary with no source checkout — the documented discovery
   path. In a checkout the files are also directly at `cli/schema/`.

5. **Compatibility policy.** A schema at version `vN` is a stable contract.

   - **Additive (compatible) — no version bump.** Adding a new optional field, a
     new `oneOf` branch, a new enum value, or loosening a constraint. Consumers
     following the repo's superset-JSON convention (ignore unknown keys) keep
     working. The schema file is edited in place at `vN`.
   - **Breaking (incompatible) — requires a new version.** Removing or renaming a
     field, changing a field's type, making an optional field required, tightening
     a value domain, or otherwise invalidating a payload a conforming consumer
     previously produced or accepted. A breaking change ships as a NEW schema
     version: a new `$id` at `vN+1` and a bumped `version` in `index.json`. The
     older version is retired only once no supported consumer depends on it.

   Because the emitted output shapes are unchanged by this ADR, every current
   schema is `v1`.

## Consequences

- Agents can validate any `--json` payload and pin the exact version they parse.
- A breaking output change can no longer land silently: the inventory gate forces
  a schema, and the policy forces a breaking one to bump the version, making the
  break a reviewed, visible artifact rather than a runtime surprise.
- Adding a result family is now a three-part reviewed change: the `CliOutput`, its
  `index.json` entry, and its committed schema. The gate refuses to go green
  otherwise. This mirrors the existing generated-artifact-plus-CI-gate discipline
  (`command-manifest.json`, `api-mirrors.json`, `plugin-format-mirrors.json`).
- The schemas are a maintenance surface: an intentional additive output change
  must edit the mapped schema in the same PR, or `json_contract.rs` goes red.
