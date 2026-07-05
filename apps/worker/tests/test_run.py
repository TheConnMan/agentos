"""Substrate selection + the local-middle-mode fail-closed credential gate.

Local middle mode (Docker substrate) defaults to a real model; fake model is an
explicit opt-in. A Docker worker with neither a model credential nor
AGENTOS_FAKE_MODEL must fail loudly instead of silently degrading to a fake.
"""

from __future__ import annotations

import pytest
from agentos_worker.config import WorkerConfig
from agentos_worker.run import _sandbox_client
from agentos_worker.sandbox import DockerSandboxClient, SubstrateConfig

_SUB = SubstrateConfig(namespace="default", warm_pool="pool")


def test_docker_without_credential_or_fake_fails_loudly() -> None:
    with pytest.raises(SystemExit) as exc:
        _sandbox_client(WorkerConfig(), {"AGENTOS_SANDBOX_SUBSTRATE": "docker"}, _SUB)
    msg = str(exc.value)
    assert "CLAUDE_CODE_OAUTH_TOKEN" in msg  # tells the user how to fix it
    assert "AGENTOS_FAKE_MODEL" in msg


def test_docker_with_real_credential_builds_docker_client() -> None:
    client = _sandbox_client(
        WorkerConfig(),
        {"AGENTOS_SANDBOX_SUBSTRATE": "docker", "CLAUDE_CODE_OAUTH_TOKEN": "sk-PLACEHOLDER"},
        _SUB,
    )
    assert isinstance(client, DockerSandboxClient)


def test_docker_with_explicit_fake_model_builds_docker_client() -> None:
    client = _sandbox_client(
        WorkerConfig(fake_model=True), {"AGENTOS_SANDBOX_SUBSTRATE": "docker"}, _SUB
    )
    assert isinstance(client, DockerSandboxClient)
