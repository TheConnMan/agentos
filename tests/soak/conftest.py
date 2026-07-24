"""Fixtures and import wiring for the soak suite.

The cluster-touching fixtures (``substrate``, ``pool_ready``) are only requested
by the gated scenario in ``test_soak_resilience.py``; when ``CURIE_SOAK`` is
unset that module skips at collection time via its own ``pytestmark`` and these
fixtures never run, so the suite stays clean offline. The pure-helper unit tests
request none of these fixtures and always run.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

# importlib import mode does not add the test directory to sys.path, so make the
# sibling ``harness`` module importable from every test module in this folder.
sys.path.insert(0, str(Path(__file__).parent))

from harness import SoakConfig, kubectl  # noqa: E402

# The scenario module carries the enforcing skip guard; this documents the same
# opt-in gate at the package level (module markers on conftest do not propagate,
# so the real guard lives in test_soak_resilience.py).
pytestmark = pytest.mark.skipif(
    os.environ.get("CURIE_SOAK") != "1",
    reason="soak/chaos suite; set CURIE_SOAK=1 with a standing cluster + dev stack",
)


@pytest.fixture(scope="session")
def cfg() -> SoakConfig:
    return SoakConfig.from_env()


@pytest.fixture(scope="session")
def substrate(cfg: SoakConfig) -> Iterator[object]:
    """A real substrate over the standing cluster and dev-stack Valkey.

    Mirrors the sandbox e2e template's ``substrate`` fixture: real ``redis``
    client, a soak-scoped key prefix, and teardown that scans and deletes the
    prefix so a run leaves no route keys behind.
    """

    import redis
    from curie_worker.sandbox import (
        AffinityStore,
        KubernetesSandboxClient,
        SandboxSubstrate,
        SubstrateConfig,
    )

    client = redis.Redis(
        host=cfg.valkey_host,
        port=cfg.valkey_port,
        password=cfg.valkey_password,
    )
    client.ping()
    prefix = "soak:curie:sandbox"
    config = SubstrateConfig(
        namespace=cfg.namespace,
        warm_pool=cfg.pool,
        claim_timeout_seconds=120.0,
        poll_interval_seconds=0.05,
        key_prefix=prefix,
    )
    yield SandboxSubstrate(
        KubernetesSandboxClient(cfg.namespace),
        AffinityStore(client, key_prefix=prefix),
        config,
    )
    keys = list(client.scan_iter(match=f"{prefix}:*"))
    if keys:
        client.delete(*keys)
    client.close()


@pytest.fixture
def pool_ready(cfg: SoakConfig) -> None:
    """Block until the warm pool has enough ready replicas for the run.

    The soak claims ``concurrency`` distinct threads plus a ``batch`` burst, so
    the pool must be able to hand out that many sandboxes without starving.
    """

    wanted = cfg.concurrency + cfg.batch
    deadline = time.monotonic() + 300.0
    while time.monotonic() < deadline:
        raw = kubectl(cfg, "get", "sandboxwarmpool", cfg.pool, "-o", "json")
        status = json.loads(raw).get("status") or {}
        if status.get("readyReplicas", 0) >= wanted:
            return
        time.sleep(2)
    raise AssertionError(
        f"warm pool {cfg.pool} never reached readyReplicas>={wanted}"
    )
