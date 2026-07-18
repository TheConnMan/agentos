"""Regression tests for WorkerConfig env-source resolution.

``populate_by_name=True`` lets tests construct the config with field-name
kwargs, but it must NOT make the env source read the bare uppercased field name
as a fallback for a field that carries a ``validation_alias``. An aliased field
must read only its ``AGENTOS_*`` alias; a stray generic env var (``API_KEY``,
``CREDENTIALS``, ...) in the pod env must be ignored, as it was before the
BaseSettings refactor.
"""

from __future__ import annotations

import os
import socket

import pytest
from agentos_worker.config import WorkerConfig


def _clear_all_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete every env var the config could read, for a clean-env baseline.

    ``BaseSettings`` reads the process environment for every field (aliased
    fields via their ``validation_alias``, the rest via the uppercased field
    name). The kernel suite runs against real Valkey/Postgres, so vars like
    ``VALKEY_HOST``/``DATABASE_URL`` may be set in the ambient env; strip them
    all so the defaults assertions below see only the code defaults.
    """
    for name, field in WorkerConfig.model_fields.items():
        alias = field.validation_alias
        key = alias if isinstance(alias, str) else name.upper()
        monkeypatch.delenv(key, raising=False)


# Every env var the OLD hand-rolled ``WorkerConfig.from_env`` read (on
# ``origin/main``), paired with a distinct sentinel and the value the field
# should hold after coercion. This is the parity oracle: the names are the exact
# old ones, so the override test proves no name drifted and no var was dropped.
_WORKER_OVERRIDES: dict[str, tuple[str, str, object]] = {
    # env var name -> (field name, raw env value, expected coerced value)
    "VALKEY_HOST": ("valkey_host", "valkey.host.example", "valkey.host.example"),
    "VALKEY_PORT": ("valkey_port", "6380", 6380),
    "VALKEY_PASSWORD": ("valkey_password", "vk-pass", "vk-pass"),
    "VALKEY_DB": ("valkey_db", "7", 7),
    "SLACK_BOT_TOKEN": ("slack_bot_token", "xoxb-sentinel", "xoxb-sentinel"),
    "SLACK_API_BASE_URL": (
        "slack_api_base_url",
        "http://slack.stub:9",
        "http://slack.stub:9",
    ),
    "DATABASE_URL": (
        "database_url",
        "postgresql+asyncpg://u:p@db:5432/x",
        "postgresql+asyncpg://u:p@db:5432/x",
    ),
    "DB_SCHEMA": ("db_schema", "myschema", "myschema"),
    "AGENTOS_PLUGIN_DIR": ("bundle_plugin_dir", "/custom/bundles", "/custom/bundles"),
    "AGENTOS_FAKE_MODEL": ("fake_model", "true", True),
    "AGENTOS_SHIMMER": ("shimmer", "yes", True),
    "AGENTOS_CREDENTIALS": ("credentials", "cred-sentinel", "cred-sentinel"),
    "AGENTOS_MODEL_BASE_URL": (
        "model_base_url",
        "http://model.local:1",
        "http://model.local:1",
    ),
    "AGENTOS_MODEL": ("model", "claude-sentinel", "claude-sentinel"),
    "AGENTOS_EVAL_STREAM": ("eval_stream", "sentinel:evals", "sentinel:evals"),
    "AGENTOS_EVAL_CONSUMER_GROUP": (
        "eval_consumer_group",
        "sentinel-eval-workers",
        "sentinel-eval-workers",
    ),
    "S3_ENDPOINT_URL": ("s3_endpoint_url", "http://s3.local:2", "http://s3.local:2"),
    "S3_ACCESS_KEY": ("s3_access_key", "ak-sentinel", "ak-sentinel"),
    "S3_SECRET_KEY": ("s3_secret_key", "sk-sentinel", "sk-sentinel"),
    "S3_REGION": ("s3_region", "eu-west-9", "eu-west-9"),
    "BUNDLE_BUCKET": ("bundle_bucket", "sentinel-bundles", "sentinel-bundles"),
    "AGENTOS_API_URL": (
        "api_base_url",
        "http://api.local:3",
        "http://api.local:3",
    ),
    "AGENTOS_API_KEY": ("api_key", "key-sentinel", "key-sentinel"),
    "LANGFUSE_HOST": ("langfuse_host", "http://lf.local:4", "http://lf.local:4"),
    "LANGFUSE_PUBLIC_KEY": ("langfuse_public_key", "pk-sentinel", "pk-sentinel"),
    "LANGFUSE_SECRET_KEY": ("langfuse_secret_key", "sk-lf-sentinel", "sk-lf-sentinel"),
    "AGENTOS_STREAM": ("stream", "sentinel:runs", "sentinel:runs"),
    "AGENTOS_CONSUMER_GROUP": (
        "consumer_group",
        "sentinel-workers",
        "sentinel-workers",
    ),
    "AGENTOS_CONSUMER_NAME": (
        "consumer_name",
        "sentinel-consumer",
        "sentinel-consumer",
    ),
    "AGENTOS_MAX_ATTEMPTS": ("max_attempts", "9", 9),
}


def test_aliased_field_ignores_bare_field_name_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stray bare-name env var must not leak into an aliased field."""
    monkeypatch.setenv("API_KEY", "stray")
    monkeypatch.setenv("CREDENTIALS", "stray-creds")

    config = WorkerConfig()

    assert config.api_key == "agentos-dev-key"  # the default, not "stray"
    assert config.credentials == ""  # the default, not "stray-creds"


def test_aliased_field_reads_its_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """The intended AGENTOS_* alias is still read from the env."""
    monkeypatch.setenv("AGENTOS_API_KEY", "intended")
    monkeypatch.setenv("AGENTOS_CREDENTIALS", "intended-creds")

    config = WorkerConfig()

    assert config.api_key == "intended"
    assert config.credentials == "intended-creds"


def test_alias_wins_over_bare_field_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """With both set, only the alias is read and the bare name is ignored."""
    monkeypatch.setenv("API_KEY", "stray")
    monkeypatch.setenv("AGENTOS_API_KEY", "intended")

    assert WorkerConfig().api_key == "intended"


def test_api_url_accepts_the_deprecated_base_url_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#496: the platform API base URL is canonically AGENTOS_API_URL, but the
    historical AGENTOS_API_BASE_URL still resolves for one release, and the
    canonical name wins when both are set."""
    monkeypatch.setenv("AGENTOS_API_BASE_URL", "http://deprecated:8000")
    assert WorkerConfig().api_base_url == "http://deprecated:8000"

    monkeypatch.setenv("AGENTOS_API_URL", "http://canonical:8000")
    assert WorkerConfig().api_base_url == "http://canonical:8000"


def test_field_name_kwargs_still_populate() -> None:
    """populate_by_name construction (used by tests) is unchanged."""
    config = WorkerConfig(fake_model=True, api_key="x", credentials="c")

    assert config.fake_model is True
    assert config.api_key == "x"
    assert config.credentials == "c"


def test_non_aliased_field_still_reads_plain_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fields without an alias keep reading their uppercased field name."""
    monkeypatch.setenv("VALKEY_HOST", "valkey.internal")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/agentos"
    )

    config = WorkerConfig()

    assert config.valkey_host == "valkey.internal"
    assert config.database_url == "postgresql+asyncpg://u:p@db:5432/agentos"


# --- Env-var parity vs the pre-pydantic from_env (review #178) ---------------


def test_defaults_parity_with_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clean env: every field must equal the exact default the old from_env produced.

    Comprehensive check -- every field is enumerated. Config drift on a default
    is a silent prod break, so this locks each default to the value the
    hand-rolled ``WorkerConfig.from_env`` (on ``origin/main``) resolved to.
    """
    _clear_all_config_env(monkeypatch)

    config = WorkerConfig()

    # Valkey
    assert config.valkey_host == "localhost"
    assert config.valkey_port == 6379
    assert config.valkey_password == ""
    assert config.valkey_db == 0
    # Slack
    assert config.slack_bot_token == ""
    assert config.slack_api_base_url == ""
    # Postgres
    assert (
        config.database_url
        == "postgresql+asyncpg://postgres:postgres@localhost:25432/postgres"
    )
    assert config.db_schema == "agentos"
    # Deployment-to-runtime binding
    assert config.bundle_plugin_dir == "/bundles/current"
    assert config.default_max_usd_per_day == 10.0
    assert config.default_max_output_tokens_per_run == 100000
    # Runner model + credentials
    assert config.fake_model is False
    assert config.credentials == ""
    assert config.model_base_url == ""
    assert config.model == ""
    # Shimmer
    assert config.shimmer is False
    # Stream / consumer group
    assert config.stream == "agentos:runs"
    assert config.consumer_group == "agentos-workers"
    # Read loop
    assert config.read_count == 16
    assert config.read_block_ms == 5000
    # Per-thread lock
    assert config.lock_ttl_ms == 120000
    assert config.lock_acquire_timeout_s == 45.0
    assert config.lock_poll_interval_s == 0.02
    # Retry
    assert config.max_attempts == 3
    assert config.retry_backoff_base_s == 1.0
    assert config.retry_backoff_max_s == 20.0
    # Markers
    assert config.idempotency_ttl_s == 86400
    # Crash recovery
    assert config.reclaim_min_idle_ms == 900000
    assert config.reclaim_interval_s == 30.0
    # Slack edit throttle
    assert config.slack_edit_min_interval_s == 0.7
    # Runner HTTP timeouts
    assert config.runner_connect_timeout_s == 10.0
    assert config.runner_total_timeout_s == 600.0
    # Eval stream
    assert config.eval_stream == "agentos:evals"
    assert config.eval_consumer_group == "agentos-eval-workers"
    # MinIO / S3
    assert config.s3_endpoint_url == "http://localhost:29000"
    assert config.s3_access_key == "minio"
    assert config.s3_secret_key == "miniosecret"
    assert config.s3_region == "us-east-1"
    assert config.bundle_bucket == "agentos-bundles"
    # Platform API
    assert config.api_base_url == "http://localhost:8000"
    assert config.api_key == "agentos-dev-key"
    assert config.report_max_attempts == 3
    assert config.report_backoff_base_s == 0.5
    # Langfuse
    assert config.langfuse_host == "http://localhost:23000"
    assert config.langfuse_public_key == "pk-lf-agentos-dev"
    assert config.langfuse_secret_key == "sk-lf-agentos-dev"
    # Key prefix
    assert config.key_prefix == "agentos:worker"

    # Factory-defaulted names have no static default: the old from_env produced
    # ``f"{hostname}-{pid}"`` via ``_default_consumer_name``. Assert that shape.
    expected_consumer = f"{socket.gethostname()}-{os.getpid()}"
    assert config.consumer_name == expected_consumer
    assert config.eval_consumer_name == expected_consumer


def test_overrides_parity_with_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every env var the old from_env read, set to a sentinel under its EXACT old
    name, must be read into the right field with the right coercion.

    Proves no env-var name drifted in the BaseSettings port and no read was
    dropped.
    """
    _clear_all_config_env(monkeypatch)
    for env_var, (_field, raw, _expected) in _WORKER_OVERRIDES.items():
        monkeypatch.setenv(env_var, raw)

    config = WorkerConfig()

    for env_var, (field, _raw, expected) in _WORKER_OVERRIDES.items():
        actual = getattr(config, field)
        assert actual == expected, f"{env_var} -> {field}: {actual!r} != {expected!r}"
        # Coercion parity: ints/bools must be the coerced type, not a raw str.
        assert type(actual) is type(expected), (
            f"{env_var} -> {field}: type {type(actual)} != {type(expected)}"
        )


# --- Operator-scoped model wire declaration (#514) ---------------------------
#
# Two new fields mirroring model_base_url: they read only their AGENTOS_* alias
# and default to "" (not declared). They are deliberately absent from the
# _WORKER_OVERRIDES parity oracle above -- that dict pins the vars the old
# hand-rolled from_env read, and these are new, not a port of anything.


def test_model_api_backend_and_env_key_default_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Undeclared is the default, so the producer emits nothing and the runner
    keeps its own pre-#514 defaults."""
    _clear_all_config_env(monkeypatch)

    config = WorkerConfig()

    assert config.model_api_backend == ""
    assert config.model_env_key == ""


def test_worker_config_reads_model_api_backend_and_env_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("AGENTOS_MODEL_API_BACKEND", "messages")
    monkeypatch.setenv("AGENTOS_MODEL_ENV_KEY", '["ANTHROPIC_AUTH_TOKEN"]')

    config = WorkerConfig()

    assert config.model_api_backend == "messages"
    assert config.model_env_key == '["ANTHROPIC_AUTH_TOKEN"]'


def test_model_api_backend_and_env_key_ignore_bare_field_name_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aliased like every other AGENTOS_* knob: a stray bare-name env var in the
    pod env must not leak in."""
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("MODEL_API_BACKEND", "chat_completions")
    monkeypatch.setenv("MODEL_ENV_KEY", "STRAY_NAME")

    config = WorkerConfig()

    assert config.model_api_backend == ""
    assert config.model_env_key == ""


def test_model_api_backend_and_env_key_populate_by_field_name() -> None:
    """populate_by_name construction (used by the binding tests) works."""
    config = WorkerConfig(model_api_backend="messages", model_env_key="MY_PROVIDER_KEY")

    assert config.model_api_backend == "messages"
    assert config.model_env_key == "MY_PROVIDER_KEY"


# --- Runner-facing API base (#678) -------------------------------------------
#
# A field distinct from api_base_url (the worker's self-dial URL): the API base a
# SPAWNED RUNNER dials. Defaults to "" (undivided) and reads only its AGENTOS_*
# alias. Kept out of the _WORKER_OVERRIDES parity oracle -- like the #514 fields,
# it is new, not a port of the old hand-rolled from_env.


def test_runner_api_base_url_defaults_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Undivided is the default: the runner reaches the API at the worker's own
    self-dial URL (k8s in-cluster, single-host local)."""
    _clear_all_config_env(monkeypatch)

    config = WorkerConfig()

    assert config.runner_api_base_url == ""


def test_worker_config_reads_runner_api_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("AGENTOS_RUNNER_API_URL", "http://agentos-api:8000")

    config = WorkerConfig()

    assert config.runner_api_base_url == "http://agentos-api:8000"


def test_runner_api_base_url_ignores_bare_field_name_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aliased like every other AGENTOS_* knob: a stray bare-name env var in the
    pod env must not leak in."""
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("RUNNER_API_BASE_URL", "http://stray:9000")

    config = WorkerConfig()

    assert config.runner_api_base_url == ""


def test_runner_facing_api_base_url_falls_back_to_self_dial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset runner_api_base_url resolves to api_base_url, so k8s and single-host
    local -- where the runner reaches the API at the worker's URL -- are unchanged."""
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("AGENTOS_API_URL", "http://in-cluster-api:8000")

    config = WorkerConfig()

    assert config.runner_api_base_url == ""
    assert config.runner_facing_api_base_url == "http://in-cluster-api:8000"


def test_runner_facing_api_base_url_prefers_the_runner_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the two networks diverge (docker substrate), the runner-facing base
    wins over the worker's localhost self-dial URL."""
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("AGENTOS_API_URL", "http://localhost:28000")
    monkeypatch.setenv("AGENTOS_RUNNER_API_URL", "http://agentos-api:8000")

    config = WorkerConfig()

    assert config.api_base_url == "http://localhost:28000"
    assert config.runner_facing_api_base_url == "http://agentos-api:8000"


def test_agentos_dead_letter_stream_reaches_the_dead_letter_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AGENTOS_DEAD_LETTER_STREAM (#505/#668) populates dead_letter_stream and
    is reflected by dead_letter_stream_name(), the graveyard the API's
    dead-letter watcher must agree with (see apps/api/tests/test_config_parity.py)."""
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("AGENTOS_STREAM", "operations")
    monkeypatch.setenv("AGENTOS_DEAD_LETTER_STREAM", "operations:dead")

    config = WorkerConfig()

    assert config.dead_letter_stream == "operations:dead"
    assert config.dead_letter_stream_name() == "operations:dead"


# --- Per-service bool divergence (review #178) -------------------------------
#
# The old worker ``_b`` accepted only ("1", "true", "yes") as truthy -- notably
# NOT "on", unlike the dispatcher's ``_set_bool``. These lock that divergence.


@pytest.mark.parametrize("token", ["1", "true", "yes", "TRUE", "Yes", " yes "])
def test_bool_shared_truthy_tokens(
    monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    """The truthy set shared with the dispatcher parses to True (case/space-insensitive)."""
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("AGENTOS_SHIMMER", token)
    monkeypatch.setenv("AGENTOS_FAKE_MODEL", token)

    config = WorkerConfig()

    assert config.shimmer is True
    assert config.fake_model is True


@pytest.mark.parametrize("token", ["on", "ON", "0", "no", "off", "", "maybe"])
def test_bool_worker_rejects_on_and_falsy_tokens(
    monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    """The worker does NOT treat "on" as truthy (the dispatcher does); falsy tokens are False."""
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("AGENTOS_SHIMMER", token)
    monkeypatch.setenv("AGENTOS_FAKE_MODEL", token)

    config = WorkerConfig()

    assert config.shimmer is False
    assert config.fake_model is False
