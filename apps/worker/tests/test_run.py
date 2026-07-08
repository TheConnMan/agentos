"""Substrate selection + the local-middle-mode fail-closed credential gate.

Local middle mode (Docker substrate) defaults to a real model; fake model is an
explicit opt-in. A Docker worker with neither a model credential nor
AGENTOS_FAKE_MODEL must fail loudly instead of silently degrading to a fake.
"""

from __future__ import annotations

import pytest
from agentos_worker.config import WorkerConfig
from agentos_worker.run import _sandbox_client, _substrate_config
from agentos_worker.sandbox import DockerSandboxClient, SubstrateConfig

_SUB = SubstrateConfig(namespace="default", warm_pool="pool")


def test_substrate_config_claim_timeout_defaults_to_90s() -> None:
    assert _substrate_config({}).claim_timeout_seconds == 90.0


def test_substrate_config_claim_timeout_reads_env() -> None:
    cfg = _substrate_config({"AGENTOS_CLAIM_TIMEOUT_SECONDS": "45"})
    assert cfg.claim_timeout_seconds == 45.0


def test_claim_timeout_default_stays_under_lock_ttl() -> None:
    # The claim is the dominant term in the per-thread critical section; it must
    # stay below the lock TTL so the lock never lapses mid-claim.
    assert _SUB.claim_timeout_seconds < WorkerConfig().lock_ttl_ms / 1000


def test_valkey_socket_timeout_exceeds_the_block_interval() -> None:
    # redis-py enforces the client socket_timeout on the blocking XREADGROUP, so
    # it must sit above read_block_ms or every idle read raises a timeout instead
    # of returning empty (log flood). Guard the invariant that keeps idle reads
    # quiet across any read_block_ms tuning.
    cfg = WorkerConfig()
    assert cfg.valkey_socket_timeout_s > cfg.read_block_ms / 1000


def test_docker_without_credential_or_fake_fails_loudly() -> None:
    with pytest.raises(SystemExit) as exc:
        _sandbox_client(WorkerConfig(), {"AGENTOS_SANDBOX_SUBSTRATE": "docker"}, _SUB)
    msg = str(exc.value)
    assert "CLAUDE_CODE_OAUTH_TOKEN" in msg  # tells the user how to fix it
    assert "AGENTOS_FAKE_MODEL" in msg


def test_docker_with_sdk_credential_builds_docker_client() -> None:
    client = _sandbox_client(
        WorkerConfig(),
        {"AGENTOS_SANDBOX_SUBSTRATE": "docker", "CLAUDE_CODE_OAUTH_TOKEN": "oauth-PLACEHOLDER"},
        _SUB,
    )
    assert isinstance(client, DockerSandboxClient)


def test_docker_with_agentos_credentials_reference_builds_docker_client() -> None:
    # AGENTOS_CREDENTIALS alone is a valid credential: forwarded by name and
    # mapped onto an SDK var by the runner, so the gate must accept it.
    client = _sandbox_client(
        WorkerConfig(credentials="sk-ant-PLACEHOLDER"),
        {"AGENTOS_SANDBOX_SUBSTRATE": "docker"},
        _SUB,
    )
    assert isinstance(client, DockerSandboxClient)


def test_docker_with_model_base_url_builds_docker_client_without_credential() -> None:
    client = _sandbox_client(
        WorkerConfig(model_base_url="http://ollama:11434"),
        {"AGENTOS_SANDBOX_SUBSTRATE": "docker"},
        _SUB,
    )
    assert isinstance(client, DockerSandboxClient)


def test_docker_with_explicit_fake_model_builds_docker_client() -> None:
    client = _sandbox_client(
        WorkerConfig(fake_model=True), {"AGENTOS_SANDBOX_SUBSTRATE": "docker"}, _SUB
    )
    assert isinstance(client, DockerSandboxClient)
