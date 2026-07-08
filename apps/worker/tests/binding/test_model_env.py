"""The runner model + credentials passthrough layered onto every boot env.

Pure unit tests (no DB / Valkey): both the runs binding and the eval consumer
must forward AGENTOS_FAKE_MODEL and AGENTOS_CREDENTIALS from the worker config
into the sandbox boot env, and must omit them when the config leaves them unset.
"""

from __future__ import annotations

import uuid

from agentos_worker.binding import (
    BASE_URL_ENV,
    CREDENTIALS_ENV,
    FAKE_MODEL_ENV,
    MODEL_ENV,
    BindingResolver,
    ResolvedDeployment,
    apply_model_env,
)
from agentos_worker.config import WorkerConfig
from agentos_worker.eval.stream import EvalStreamConsumer, EvalWorkItem


def _resolved() -> ResolvedDeployment:
    return ResolvedDeployment(
        agent_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        version_label="v1",
        bundle_ref="bundles/x.zip",
        max_usd_per_day=None,
        max_output_tokens_per_run=None,
    )


def test_apply_model_env_forwards_fake_and_credentials_when_set() -> None:
    env: dict[str, str] = {}
    apply_model_env(env, WorkerConfig(fake_model=True, credentials="tok-abc"))
    assert env[FAKE_MODEL_ENV] == "1"
    assert env[CREDENTIALS_ENV] == "tok-abc"


def test_apply_model_env_omits_both_when_unset() -> None:
    env: dict[str, str] = {}
    apply_model_env(env, WorkerConfig())  # defaults: fake_model=False, credentials=""
    assert FAKE_MODEL_ENV not in env
    assert CREDENTIALS_ENV not in env


def test_apply_model_env_forwards_base_url_and_model_without_fake_flag() -> None:
    base_url_env, model_env = BASE_URL_ENV, MODEL_ENV
    assert base_url_env == "ANTHROPIC_BASE_URL"
    assert model_env == "AGENTOS_MODEL"

    env: dict[str, str] = {}
    apply_model_env(
        env,
        WorkerConfig(model_base_url="http://ollama:11434", model="qwen3:4b"),
    )

    assert env[base_url_env] == "http://ollama:11434"
    assert env[model_env] == "qwen3:4b"
    assert FAKE_MODEL_ENV not in env


def test_apply_model_env_forwards_model_without_base_url() -> None:
    env: dict[str, str] = {}
    apply_model_env(env, WorkerConfig(credentials="sk-or-xyz", model="z-ai/glm-5.2"))
    assert env[MODEL_ENV] == "z-ai/glm-5.2"
    assert env[CREDENTIALS_ENV] == "sk-or-xyz"
    assert BASE_URL_ENV not in env


def test_apply_model_env_forwards_base_url_without_model() -> None:
    base_url_env, model_env = BASE_URL_ENV, MODEL_ENV

    env: dict[str, str] = {}
    apply_model_env(env, WorkerConfig(model_base_url="http://ollama:11434"))

    assert env[base_url_env] == "http://ollama:11434"
    assert model_env not in env


def test_apply_model_env_omits_base_url_and_model_by_default() -> None:
    base_url_env, model_env = BASE_URL_ENV, MODEL_ENV

    env: dict[str, str] = {}
    apply_model_env(env, WorkerConfig())

    assert base_url_env not in env
    assert model_env not in env


def test_worker_config_reads_local_model_env() -> None:
    config = WorkerConfig.from_env(
        {"AGENTOS_MODEL_BASE_URL": "http://x:1", "AGENTOS_MODEL": "qwen3:4b"}
    )

    assert config.model_base_url == "http://x:1"
    assert config.model == "qwen3:4b"


def test_binding_boot_env_carries_fake_and_credentials() -> None:
    # No engine call is made by boot_env, so a bare resolver suffices.
    resolver = BindingResolver.__new__(BindingResolver)
    resolver._config = WorkerConfig(fake_model=True, credentials="cred-1")  # type: ignore[attr-defined]

    env = resolver.boot_env(_resolved(), "thread-1")
    assert env[FAKE_MODEL_ENV] == "1"
    assert env[CREDENTIALS_ENV] == "cred-1"

    # Fake model off + no credentials -> neither leaks into the boot env.
    resolver._config = WorkerConfig()  # type: ignore[attr-defined]
    env = resolver.boot_env(_resolved(), "thread-1")
    assert FAKE_MODEL_ENV not in env
    assert CREDENTIALS_ENV not in env


def test_eval_boot_env_carries_fake_and_credentials() -> None:
    consumer = EvalStreamConsumer(
        redis=None,  # type: ignore[arg-type]
        config=WorkerConfig(fake_model=True, credentials="cred-eval"),
        bundle_store=None,  # type: ignore[arg-type]
        substrate=None,  # type: ignore[arg-type]
        reporter=None,  # type: ignore[arg-type]
        recorder=None,  # type: ignore[arg-type]
        repo_lookup=None,
    )
    item = EvalWorkItem(
        agent_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        sha="deadbeef",
        suite="smoke",
        bundle_ref="bundles/x.zip",
        requested_at="2026-07-05T00:00:00+00:00",
    )
    env = consumer._boot_env(item)
    assert env[FAKE_MODEL_ENV] == "1"
    assert env[CREDENTIALS_ENV] == "cred-eval"
