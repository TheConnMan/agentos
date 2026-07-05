"""Process entrypoint: wire the kernel and consumer, then run.

Reads the environment, builds the async Valkey client (stream, locks, markers),
a sync Valkey client for the substrate's affinity store, the sandbox substrate,
the runner HTTP client, and the Slack sink, then runs the consumer until a
signal asks it to stop. Run with ``python -m agentos_worker``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Mapping

import redis
from redis.asyncio import Redis as AsyncRedis

from .config import WorkerConfig
from .consumer import Consumer
from .kernel import Kernel
from .markers import Markers
from .runner_client import RunnerClient
from .sandbox import (
    AffinityStore,
    KubernetesSandboxClient,
    SandboxSubstrate,
    SubstrateConfig,
)
from .slack_sink import AsyncSlackSink
from .threadlock import ThreadLock


def _substrate_config(env: Mapping[str, str]) -> SubstrateConfig:
    return SubstrateConfig(
        namespace=env.get("AGENTOS_NAMESPACE", "default"),
        warm_pool=env.get("AGENTOS_WARM_POOL", "agentos-runner-pool"),
        runner_port=int(env.get("AGENTOS_RUNNER_PORT", "8080")),
    )


def build(
    config: WorkerConfig, env: Mapping[str, str]
) -> tuple[Consumer, RunnerClient, AsyncRedis]:
    async_redis: AsyncRedis = AsyncRedis(
        host=config.valkey_host,
        port=config.valkey_port,
        password=config.valkey_password or None,
        db=config.valkey_db,
        decode_responses=True,
    )
    sync_redis = redis.Redis(
        host=config.valkey_host,
        port=config.valkey_port,
        password=config.valkey_password or None,
        db=config.valkey_db,
        decode_responses=True,
    )
    sub_config = _substrate_config(env)
    substrate = SandboxSubstrate(
        KubernetesSandboxClient(sub_config.namespace),
        AffinityStore(sync_redis),
        sub_config,
    )
    runner = RunnerClient(
        connect_timeout_s=config.runner_connect_timeout_s,
        total_timeout_s=config.runner_total_timeout_s,
    )
    kernel = Kernel(
        substrate=substrate,
        runner=runner,
        sink=AsyncSlackSink(
            config.slack_bot_token, base_url=config.slack_api_base_url or None
        ),
        lock=ThreadLock(
            async_redis,
            ttl_ms=config.lock_ttl_ms,
            acquire_timeout_s=config.lock_acquire_timeout_s,
            poll_interval_s=config.lock_poll_interval_s,
        ),
        markers=Markers(async_redis, config),
        config=config,
    )
    consumer = Consumer(redis=async_redis, kernel=kernel, config=config)
    return consumer, runner, async_redis


async def _run(config: WorkerConfig, env: Mapping[str, str]) -> None:
    consumer, runner, async_redis = build(config, env)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, consumer.request_stop)

    logging.getLogger("agentos_worker").info("worker starting")
    try:
        await consumer.run()
    finally:
        await runner.close()
        await async_redis.aclose()
    logging.getLogger("agentos_worker").info("worker stopped")


def main(env: Mapping[str, str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO)
    resolved = env if env is not None else os.environ
    config = WorkerConfig.from_env(resolved)
    asyncio.run(_run(config, resolved))


if __name__ == "__main__":
    main()
