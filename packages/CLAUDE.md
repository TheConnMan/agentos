# CLAUDE.md - packages/

`packages/aci-protocol` and `packages/plugin-format` are **frozen interfaces**.
Read the root `AGENTS.md`'s "Frozen contracts: STOP and escalate" section first
-- it is not optional here.

## The rule

Every lane in this repo (runner, worker, CLI, UI) compiles against these two
packages across three languages. Pydantic models are the single source of
truth; JSON Schema, generated TypeScript, and generated Rust are committed
derivatives, never hand-edited.

**Do not change either package unilaterally from a dependent lane.** If your
task needs a change here, stop and raise it in an issue/PR first -- a contract
change lands as its own reviewed, backward-compatible change before dependent
lanes proceed.

## When a change is genuinely approved

1. Bump `aci_protocol.version.PROTOCOL_VERSION` to the class the change earns
   (the plugin-format schema has no separate version today). The ACI is
   versioned as **semver, independent of the AgentOS release**: the number
   tracks the wire contract's compatibility, not the product's cadence.
   **Backward compatibility is the default expectation.** A consumer accepts a
   wire version with the same `major.minor` under 0.x (the same `major` after
   1.0) and rejects anything else, loudly, naming both versions. Pick the bump
   from the change class:

   | Change class | 0.x today | Post-1.0 |
   |---|---|---|
   | New optional field (consumer ignores it) | patch | minor |
   | New required field | minor (breaking) | major |
   | New enum value | minor (breaking) | major |
   | Field removal | minor (breaking) | major |
   | Field rename | minor (breaking) | major |
   | Type change | minor (breaking) | major |
   | Doc/description only (no wire change) | none | none |

   Only a **new optional field is compatible** while we are in 0.x: tolerant
   consumers ignore it, so it is a patch and old consumers keep decoding. Every
   other row breaks an old consumer, so under 0.x it bumps the **minor** (the
   breaking axis before 1.0) and old consumers reject it at the version gate
   with both versions named. A new **enum value** counts as breaking even though
   it looks additive: `SessionStatus` is control-bearing, so an unknown value is
   rejected, never degraded (see ADR-0036). When a change cannot be made
   backward compatible, do not force it into a patch to keep the gate quiet:
   bump the breaking class, and if two versions must coexist on the wire, ship
   and name both explicitly rather than pretending one is compatible.
2. Regenerate every committed artifact and check for drift:
   ```bash
   ./scripts/check-contracts.sh
   ```
   This regenerates both JSON Schemas, the generated Rust crate, and the
   generated TypeScript, then diffs them against what is committed. It fails
   loudly if anything drifted and was not regenerated.
3. Commit the regenerated schema and generated types together with the model
   change, in the same commit.

## Enforcement

- `tests/test_schema_compat.py` in each package regenerates its schema
  in-process and fails if the committed copy differs -- this is the CI gate,
  not just a local nicety.
- CI additionally compiles the generated Rust (`cargo test` against
  `packages/aci-protocol/generated/rust`) and the generated TypeScript
  (`tsc --noEmit`), so a schema that "compiles in Python" but breaks either
  target still fails the build.

## Model conventions specific to these packages

- **`aci-protocol` is strict producers, tolerant consumers** (conservative in
  what it sends, liberal in what it accepts). Constructing an event with an
  unknown field is still an error -- that is where the value of strictness
  lives, catching producer mistakes at the source. But a **consumer decoding
  the wire ignores fields it does not model**, so a new optional field is
  genuinely backward compatible and a minor bump means something. The version
  gate is **compatibility-aware, not exact-match**: it accepts any wire version
  that matches the build's `major.minor` under 0.x (`major` after 1.0) and
  rejects across an incompatible boundary, naming both versions. Loosening the
  gate further (accepting an incompatible version) is a version-policy change --
  raise it in an issue/PR first, do not quietly relax a model.
- **`plugin-format` is lenient by design.** Its models use `extra="allow"`
  because real Claude Code plugin bundles carry keys this MVP does not model;
  rejecting them would reject valid bundles. Do not add strict validation
  here without checking this is compatible with the "verbatim Claude Code
  shape" mandate -- the wedge is compatibility, not schema purity.
- **Field names mirror the real Claude Code plugin format verbatim**
  (`allowed-tools`, not `tools`; `.claude-plugin/plugin.json` as the primary
  manifest location). Never invent a friendlier field name here.

## Verify

```bash
uv run pytest packages/aci-protocol/tests packages/plugin-format/tests -q
uv run ruff check packages/
uv run mypy
```
