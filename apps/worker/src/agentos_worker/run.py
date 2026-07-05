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
from dataclasses import dataclass

import httpx
import redis
from redis.asyncio import Redis as AsyncRedis
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .binding import BindingResolver
from .bundle_store import BundleStore
from .config import WorkerConfig
from .consumer import Consumer
from .eval import EvalReporter, EvalStreamConsumer, LangfuseEvalRecorder
from .kernel import Kernel
from .killswitch import KillSwitch
from .markers import Markers
from .runner_client import RunnerClient
from .sandbox import (
    AffinityStore,
    DockerSandboxClient,
    KubernetesSandboxClient,
    SandboxClient,
    SandboxSubstrate,
    SubstrateConfig,
)
from .slack_sink import AsyncSlackSink
from .threadlock import ThreadLock


@dataclass
class Runtime:
    """The wired worker: the two Valkey consumers (runs + evals) plus the
    resources whose lifetimes they share, so ``_run`` can drive and dispose them."""

    consumer: Consumer
    killswitch: KillSwitch
    eval_consumer: EvalStreamConsumer
    runner: RunnerClient
    async_redis: AsyncRedis
    eval_redis: AsyncRedis
    eval_http: httpx.AsyncClient
    engine: AsyncEngine


def _substrate_config(env: Mapping[str, str]) -> SubstrateConfig:
    return SubstrateConfig(
        namespace=env.get("AGENTOS_NAMESPACE", "default"),
        warm_pool=env.get("AGENTOS_WARM_POOL", "agentos-runner-pool"),
        runner_port=int(env.get("AGENTOS_RUNNER_PORT", "8080")),
    )


# The SDK credential env the runner authenticates a real model with; presence of
# either satisfies the local-middle-mode credential requirement.
_MODEL_CREDENTIAL_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


def _sandbox_client(
    config: WorkerConfig, env: Mapping[str, str], sub_config: SubstrateConfig
) -> SandboxClient:
    """The cluster/Docker seam, chosen by ``AGENTOS_SANDBOX_SUBSTRATE``.

    ``kubernetes`` (default) claims agent-sandbox CRs; ``docker`` boots runner
    containers locally (middle mode on a laptop, no cluster). The eval consumer
    shares the substrate this client backs, so the choice applies to both lanes.

    Local middle mode defaults to a REAL model. Fake model is an explicit
    offline/test opt-in, so a Docker worker with neither a model credential nor
    ``AGENTOS_FAKE_MODEL`` fails loudly here rather than booting a real runner
    that would fail cryptically or silently degrading to a fake. A credential can
    be an SDK var (``CLAUDE_CODE_OAUTH_TOKEN`` / ``ANTHROPIC_API_KEY``) or the ACI
    ``AGENTOS_CREDENTIALS`` reference, which the runner maps onto an SDK var.
    """
    substrate = env.get("AGENTOS_SANDBOX_SUBSTRATE", "kubernetes").lower()
    if substrate == "docker":
        has_credential = bool(config.credentials) or any(
            v in env for v in _MODEL_CREDENTIAL_ENV
        )
        if not config.fake_model and not has_credential:
            raise SystemExit(
                "Local middle mode (AGENTOS_SANDBOX_SUBSTRATE=docker) defaults to a "
                "real model, but no model credential is set. Export "
                "AGENTOS_CREDENTIALS, CLAUDE_CODE_OAUTH_TOKEN, or ANTHROPIC_API_KEY "
                "before starting the worker, or set AGENTOS_FAKE_MODEL=1 for an "
                "offline/test run."
            )
        return DockerSandboxClient(
            image=env.get("AGENTOS_RUNNER_IMAGE", "agentos-runner"),
            bundle_store=BundleStore(config),
            network=env.get("AGENTOS_DOCKER_NETWORK") or None,
            otel_endpoint=env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or None,
            default_plugin_dir=config.bundle_plugin_dir,
            environ=env,
        )
    return KubernetesSandboxClient(sub_config.namespace)


def build(config: WorkerConfig, env: Mapping[str, str]) -> Runtime:
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
        _sandbox_client(config, env, sub_config),
        AffinityStore(sync_redis),
        sub_config,
    )
    runner = RunnerClient(
        connect_timeout_s=config.runner_connect_timeout_s,
        total_timeout_s=config.runner_total_timeout_s,
    )
    engine = create_async_engine(config.database_url, pool_pre_ping=True)
    binding = BindingResolver(engine, config)
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
        binding=binding,
    )
    killswitch = KillSwitch(async_redis, on_kill=kernel.interrupt_agent)
    kernel.attach_killswitch(killswitch)
    consumer = Consumer(redis=async_redis, kernel=kernel, config=config)

    # The eval lane (F3): a second consumer group on agentos:evals, on its own
    # Valkey connection so its blocking read never stalls the runs consumer. It
    # reuses the same substrate (eval runs provision from the same warm pool) and
    # the binding resolver as its repo lookup for the /evals/report payload.
    eval_redis: AsyncRedis = AsyncRedis(
        host=config.valkey_host,
        port=config.valkey_port,
        password=config.valkey_password or None,
        db=config.valkey_db,
        decode_responses=True,
    )
    eval_http = httpx.AsyncClient(timeout=30.0)
    eval_consumer = EvalStreamConsumer(
        redis=eval_redis,
        config=config,
        bundle_store=BundleStore(config),
        substrate=substrate,
        reporter=EvalReporter(
            api_base_url=config.api_base_url,
            api_key=config.api_key,
            client=eval_http,
            max_attempts=config.report_max_attempts,
            backoff_base_s=config.report_backoff_base_s,
        ),
        recorder=LangfuseEvalRecorder(
            base_url=config.langfuse_host,
            public_key=config.langfuse_public_key,
            secret_key=config.langfuse_secret_key,
            client=eval_http,
        ),
        repo_lookup=binding,
    )
    return Runtime(
        consumer=consumer,
        killswitch=killswitch,
        eval_consumer=eval_consumer,
        runner=runner,
        async_redis=async_redis,
        eval_redis=eval_redis,
        eval_http=eval_http,
        engine=engine,
    )


async def _run(config: WorkerConfig, env: Mapping[str, str]) -> None:
    rt = build(config, env)

    loop = asyncio.get_running_loop()

    def _stop() -> None:
        rt.consumer.request_stop()
        rt.killswitch.request_stop()
        rt.eval_consumer.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)

    logging.getLogger("agentos_worker").info("worker starting")
    try:
        await asyncio.gather(
            rt.consumer.run(), rt.killswitch.run(), rt.eval_consumer.run()
        )
    finally:
        await rt.runner.close()
        await rt.eval_http.aclose()
        await rt.async_redis.aclose()
        await rt.eval_redis.aclose()
        await rt.engine.dispose()
    logging.getLogger("agentos_worker").info("worker stopped")


def main(env: Mapping[str, str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO)
    resolved = env if env is not None else os.environ
    config = WorkerConfig.from_env(resolved)
    asyncio.run(_run(config, resolved))


if __name__ == "__main__":
    main()
