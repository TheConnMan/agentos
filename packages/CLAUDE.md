# CLAUDE.md — packages/

`packages/aci-protocol` and `packages/plugin-format` are **frozen interfaces**
(owning task C1). Read the root `CLAUDE.md`'s "Frozen contracts: STOP and
escalate" section first -- it is not optional here.

## The rule

Every lane in this repo (runner, worker, CLI, UI) compiles against these two
packages across three languages. Pydantic models are the single source of
truth; JSON Schema, generated TypeScript, and generated Rust are committed
derivatives, never hand-edited.

**Do not change either package unilaterally from a dependent lane.** If your
task needs a change here, stop and escalate to the orchestrator -- it lands
the change as its own reviewed PR before dependent lanes proceed.

## When a change is genuinely approved

1. Bump the relevant version constant (`aci_protocol.version.PROTOCOL_VERSION`
   for the ACI; the plugin-format schema has no separate version today).
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

- **`aci-protocol` is strict.** The wire contract rejects unknown fields
  (`deny_unknown_fields` equivalent in both Python and generated Rust) and
  rejects any `version` other than the exact `PROTOCOL_VERSION` (no
  same-major looseness in the 0.x line). If you are tempted to loosen this
  for a consumer's convenience, that is a version-policy change -- escalate,
  do not quietly relax a model.
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
