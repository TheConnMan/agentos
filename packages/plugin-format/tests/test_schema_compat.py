"""The plugin-format compat gate: committed schema must match the models."""

from plugin_format.schema_export import render_schema, schema_path


def test_committed_json_schema_is_current() -> None:
    committed = schema_path().read_text(encoding="utf-8")
    assert render_schema() == committed, (
        "plugin-format JSON Schema is stale; run scripts/check-contracts.sh and commit"
    )
