"""The eval-case compat gate: committed schema must match the models.

Mirrors packages/plugin-format/tests/test_schema_compat.py.
"""

from agentos_worker.eval.schema_export import render_schema, schema_path


def test_committed_json_schema_is_current() -> None:
    committed = schema_path().read_text(encoding="utf-8")
    assert render_schema() == committed, (
        "eval-case JSON Schema is stale; run scripts/check-contracts.sh and commit"
    )
