"""RunnerConfig env parsing."""

from agentos_runner import RunnerConfig

_BASE = {
    "AGENTOS_PLUGIN_DIR": "/bundle",
    "AGENTOS_SESSION_ID": "sess-1",
    "AGENTOS_SANDBOX_ID": "sbx-1",
    "AGENTOS_BUDGET": '{"max_output_tokens_per_run": 1000, "max_usd_per_day": 5.0}',
}


def test_parses_budget_and_defaults() -> None:
    config = RunnerConfig.from_env(dict(_BASE))
    assert config.ceiling == 1000
    assert config.max_usd_per_day == 5.0
    assert config.max_turns == 20
    assert config.port == 8080
    assert config.idempotent_tools is None
    assert config.history_ref is None


def test_history_ref_is_not_derived_from_memory_ref() -> None:
    # A memory ref is an externalized-memory pointer, not an SDK resume id, so it
    # must not become the rehydrate ref.
    env = dict(_BASE, AGENTOS_MEMORY_REF="s3://mem/thread")
    assert RunnerConfig.from_env(env).history_ref is None


def test_explicit_history_ref_wins() -> None:
    env = dict(_BASE, AGENTOS_MEMORY_REF="s3://mem", AGENTOS_HISTORY_REF="s3://hist")
    assert RunnerConfig.from_env(env).history_ref == "s3://hist"


def test_idempotent_tools_override_parsed() -> None:
    env = dict(_BASE, AGENTOS_IDEMPOTENT_TOOLS="Read, Custom , Grep")
    assert RunnerConfig.from_env(env).idempotent_tools == ["Read", "Custom", "Grep"]


def test_runner_token_parsed_from_env() -> None:
    env = dict(_BASE, AGENTOS_RUNNER_TOKEN="abc123")
    assert RunnerConfig.from_env(env).runner_token == "abc123"


def test_runner_token_absent_is_none() -> None:
    assert RunnerConfig.from_env(dict(_BASE)).runner_token is None


def test_runner_token_empty_string_is_none() -> None:
    # An empty env value is treated as unset, so a stray empty var never turns on
    # enforcement with an unusable token.
    env = dict(_BASE, AGENTOS_RUNNER_TOKEN="")
    assert RunnerConfig.from_env(env).runner_token is None
