"""The artifact-sync gate: committed schema and generated Rust match the models.

These tests check *artifact sync* only: if a model changes without regenerating
the committed schema or Rust, they fail. Regenerate with
``scripts/check-contracts.sh`` (or the two module entry points) and commit the
result. They do not, and cannot, judge backward compatibility -- a model change
regenerates both sides and stays green. Compatibility is a policy defined by the
semver change-class table (packages/CLAUDE.md); an *unbumped* wire change is
caught separately by the wire-lock gate in ``tests/test_wire_lock.py``.

The generated TypeScript compiles under tsc in CI (it needs a Node toolchain, so
it is not regenerated here); its input is the same committed schema this gate
pins, so a drifted schema is caught here before TypeScript can diverge.
"""

from aci_protocol.rust_export import crate_dir, render_rust
from aci_protocol.schema_export import render_schema, schema_path


def test_committed_json_schema_is_current() -> None:
    committed = schema_path().read_text(encoding="utf-8")
    assert render_schema() == committed, (
        "aci-protocol JSON Schema is stale; run scripts/check-contracts.sh and commit"
    )


def test_committed_rust_is_current() -> None:
    committed = (crate_dir() / "src" / "lib.rs").read_text(encoding="utf-8")
    assert render_rust() == committed, (
        "generated Rust is stale; run scripts/check-contracts.sh and commit"
    )
