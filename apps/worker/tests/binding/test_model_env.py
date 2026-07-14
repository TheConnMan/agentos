"""The runner model + credentials passthrough layered onto every boot env.

Pure unit tests (no DB / Valkey): both the runs binding and the eval consumer
must forward AGENTOS_FAKE_MODEL and AGENTOS_CREDENTIALS from the worker config
into the sandbox boot env, and must omit them when the config leaves them unset.
"""

from __future__ import annotations

import uuid

import pytest
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


def _resolved(model: str | None = None) -> ResolvedDeployment:
    return ResolvedDeployment(
        agent_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        version_label="v1",
        bundle_ref="bundles/x.zip",
        max_usd_per_day=None,
        max_output_tokens_per_run=None,
        model=model,
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


def test_apply_model_env_leaves_sdk_creds_absent_without_credentials() -> None:
    # No BYO credential -> the SDK vars are neither added nor blanked, so the
    # legacy ambient-token real-model path still flows the operator's token.
    env: dict[str, str] = {}
    apply_model_env(env, WorkerConfig())
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env


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


def test_worker_config_reads_local_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_MODEL_BASE_URL", "http://x:1")
    monkeypatch.setenv("AGENTOS_MODEL", "qwen3:4b")
    config = WorkerConfig()

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


# --- Per-agent model selection (#254) -----------------------------------------


def test_apply_model_env_model_override_wins_over_config() -> None:
    env: dict[str, str] = {}
    apply_model_env(
        env, WorkerConfig(model="platform-default"), model_override="agent-glm-5"
    )
    assert env[MODEL_ENV] == "agent-glm-5"


def test_apply_model_env_model_override_none_falls_back_to_config() -> None:
    env: dict[str, str] = {}
    apply_model_env(
        env, WorkerConfig(model="platform-default"), model_override=None
    )
    assert env[MODEL_ENV] == "platform-default"


def test_apply_model_env_model_override_without_config_model() -> None:
    env: dict[str, str] = {}
    apply_model_env(env, WorkerConfig(), model_override="agent-kimi")
    assert env[MODEL_ENV] == "agent-kimi"


def test_binding_boot_env_forwards_per_agent_model() -> None:
    resolver = BindingResolver.__new__(BindingResolver)
    resolver._config = WorkerConfig(model="platform-default")  # type: ignore[attr-defined]

    # A pinned per-agent model overrides the worker default.
    env = resolver.boot_env(_resolved(model="agent-deepseek-v4"), "thread-1")
    assert env[MODEL_ENV] == "agent-deepseek-v4"

    # No per-agent model -> the platform default still applies.
    env = resolver.boot_env(_resolved(model=None), "thread-1")
    assert env[MODEL_ENV] == "platform-default"


# --- Per-sandbox runner token minting (issue #63) -----------------------------
# The env-var name is the cross-package contract with the runner; asserted by its
# literal string so this test file never depends on a constant that only exists
# after the feature lands (which would break collection of the whole module).
RUNNER_TOKEN_ENV = "AGENTOS_RUNNER_TOKEN"


def test_binding_boot_env_mints_runner_token() -> None:
    resolver = BindingResolver.__new__(BindingResolver)
    resolver._config = WorkerConfig()  # type: ignore[attr-defined]

    env = resolver.boot_env(_resolved(), "thread-1")
    assert env.get(RUNNER_TOKEN_ENV), "boot_env must mint a non-empty runner token"


def test_binding_boot_env_token_is_unique_per_claim() -> None:
    resolver = BindingResolver.__new__(BindingResolver)
    resolver._config = WorkerConfig()  # type: ignore[attr-defined]

    first = resolver.boot_env(_resolved(), "thread-1")[RUNNER_TOKEN_ENV]
    second = resolver.boot_env(_resolved(), "thread-1")[RUNNER_TOKEN_ENV]
    assert first != second  # a fresh token is minted per claim


def test_binding_boot_env_token_survives_credentials() -> None:
    # Regression guard on the PR #109 apply_model_env credential pop: it strips
    # ambient SDK creds when AGENTOS_CREDENTIALS is set, but must not eat the
    # runner token (which is not a model credential).
    resolver = BindingResolver.__new__(BindingResolver)
    resolver._config = WorkerConfig(fake_model=True, credentials="cred-1")  # type: ignore[attr-defined]

    env = resolver.boot_env(_resolved(), "thread-1")
    assert env.get(RUNNER_TOKEN_ENV), "the runner token must survive credential selection"
    assert env[CREDENTIALS_ENV] == "cred-1"


# --- Conversation-history ref delivery (#20, ADR-0029) ------------------------
# The env-var names are the cross-package contract with the runner; asserted by
# their literal strings so this file never depends on a constant that only exists
# after the feature lands.
HISTORY_REF_ENV = "AGENTOS_HISTORY_REF"
HISTORY_TOKEN_ENV = "AGENTOS_HISTORY_TOKEN"


def test_binding_boot_env_sets_per_thread_transcript_ref() -> None:
    resolver = BindingResolver.__new__(BindingResolver)
    config = WorkerConfig()
    resolver._config = config  # type: ignore[attr-defined]

    resolved = _resolved()
    env = resolver.boot_env(resolved, "1720000000.000100")
    base = config.api_base_url.rstrip("/")
    assert env[HISTORY_REF_ENV] == (
        f"{base}/agents/{resolved.agent_id}/state/transcript/1720000000.000100"
    )
    # The API key is forwarded as the history token (shared with memory today).
    assert env[HISTORY_TOKEN_ENV] == config.api_key


def test_binding_boot_env_history_ref_is_deterministic_per_thread() -> None:
    # Every claim for a thread yields the same ref, so a fresh, a restarted, and a
    # resumed sandbox all rehydrate the same transcript (#20) with no special path.
    resolver = BindingResolver.__new__(BindingResolver)
    resolver._config = WorkerConfig()  # type: ignore[attr-defined]

    resolved = _resolved()
    first = resolver.boot_env(resolved, "t-1")[HISTORY_REF_ENV]
    second = resolver.boot_env(resolved, "t-1")[HISTORY_REF_ENV]
    assert first == second


def test_binding_boot_env_url_encodes_thread_key() -> None:
    # A thread key with reserved characters must not escape the transcript key
    # path; it is percent-encoded (safe="") so slashes cannot inject a new path.
    resolver = BindingResolver.__new__(BindingResolver)
    resolver._config = WorkerConfig()  # type: ignore[attr-defined]

    env = resolver.boot_env(_resolved(), "weird/../key")
    assert "/state/transcript/weird%2F..%2Fkey" in env[HISTORY_REF_ENV]


def test_binding_boot_env_omits_history_token_without_api_key() -> None:
    resolver = BindingResolver.__new__(BindingResolver)
    resolver._config = WorkerConfig(api_key="")  # type: ignore[attr-defined]

    env = resolver.boot_env(_resolved(), "t-1")
    assert HISTORY_TOKEN_ENV not in env
