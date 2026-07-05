import pytest
from aci_protocol import Budget, OtelConfig, SessionConfig
from pydantic import ValidationError


def _full_config() -> SessionConfig:
    return SessionConfig(
        plugin_dir="/plugins/demo",
        session_id="thread-123",
        sandbox_id="sbx-abc",
        budget=Budget(max_output_tokens_per_run=4096, task_budget_hint=2000, max_usd_per_day=5.0),
        memory_ref="s3://bucket/memory",
        credentials_ref="k8s://secret/demo",
        otel=OtelConfig(endpoint="http://collector:4318", protocol="http/protobuf"),
    )


def test_to_env_and_from_env_roundtrip() -> None:
    config = _full_config()
    env = config.to_env()
    assert env["AGENTOS_PLUGIN_DIR"] == "/plugins/demo"
    assert env["AGENTOS_SESSION_ID"] == "thread-123"
    assert env["AGENTOS_SANDBOX_ID"] == "sbx-abc"
    assert env["AGENTOS_MEMORY_REF"] == "s3://bucket/memory"
    assert env["AGENTOS_CREDENTIALS"] == "k8s://secret/demo"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://collector:4318"
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"

    assert SessionConfig.from_env(env) == config


def test_optional_fields_are_omitted_from_env() -> None:
    config = SessionConfig(
        plugin_dir="/p",
        session_id="s",
        sandbox_id="b",
        budget=Budget(max_output_tokens_per_run=100, max_usd_per_day=1.0),
    )
    env = config.to_env()
    assert "AGENTOS_MEMORY_REF" not in env
    assert "AGENTOS_CREDENTIALS" not in env
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env
    assert SessionConfig.from_env(env) == config


def test_budget_travels_as_json() -> None:
    config = _full_config()
    env = config.to_env()
    assert '"max_output_tokens_per_run":4096' in env["AGENTOS_BUDGET"]


def test_missing_required_env_var_raises() -> None:
    with pytest.raises(KeyError):
        SessionConfig.from_env({"AGENTOS_PLUGIN_DIR": "/p"})


def test_malformed_budget_json_raises() -> None:
    env = _full_config().to_env()
    env["AGENTOS_BUDGET"] = "{not json"
    with pytest.raises(ValidationError):
        SessionConfig.from_env(env)
