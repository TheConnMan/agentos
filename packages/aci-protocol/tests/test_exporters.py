"""Exporter regressions: the version guard must survive the Literal removal.

Decision 5 -- the highest-risk silent failure in this change. Both exporters
detect the version field by introspecting the single-valued Literal. Once
``version`` becomes a plain ``str``, that introspection stops matching and the
guard vanishes green: the schema quietly makes ``version`` optional, and the Rust
reader quietly stops checking it. These tests assert the rendered artifacts, not
the private helpers, because the rendered artifact is the real contract.
"""

from aci_protocol.rust_export import render_rust
from aci_protocol.schema_export import build_schema


def test_version_field_is_required_in_the_exported_schema() -> None:
    final = build_schema()["$defs"]["Final"]
    assert "version" in final["required"]
    # It is no longer a const; it is a semver-pattern string. A const value would
    # re-pin the exact version and defeat the whole compatibility range.
    assert "const" not in final["properties"]["version"]


def test_generated_rust_guards_the_version() -> None:
    rust = render_rust()
    lines = rust.splitlines()
    version_fields = [i for i, line in enumerate(lines) if line.strip() == "version: String,"]
    assert version_fields, "no version field found in the generated Rust"
    for i in version_fields:
        preceding = lines[i - 1].strip()
        # The compatibility deserializer must decorate every version field, and
        # #[serde(default)] must NOT -- a defaulted version is the silent gutting.
        assert preceding == '#[serde(deserialize_with = "require_compatible_protocol_version")]', (
            f"version field is not guarded; preceding line was {preceding!r}"
        )
