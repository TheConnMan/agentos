from channel_protocol.schema_export import render_schema, schema_path


def test_committed_schema_is_current() -> None:
    assert schema_path().read_text(encoding="utf-8") == render_schema(), (
        "channel protocol schema is stale; run python -m channel_protocol.schema_export"
    )
