"""The compat gate: committed schema and generated Rust must match the models.

If a model changes without regenerating the committed artifacts, these tests
fail, which is the drift detection the frozen contract relies on. Regenerate
with ``scripts/check-contracts.sh`` (or the two module entry points) and commit
the result, bumping PROTOCOL_VERSION per the frozen-interface rule.

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
