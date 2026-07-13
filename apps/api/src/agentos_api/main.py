"""FastAPI application factory and shared-resource lifespan.

The engine, sessionmaker, httpx client, and Langfuse client are created once at
startup and stored on app.state; dependencies (deps.py) read them per request.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as redis
from fastapi import FastAPI

from .approvals import ApprovalNotifier
from .config import get_settings
from .db import create_engine, create_sessionmaker
from .evalqueue import EvalQueue
from .github_checks import GitHubStatusReporter
from .k8s import build_lazy_pod_lister, build_lazy_pod_log_reader
from .killswitch import KillSwitch
from .langfuse import LangfuseClient
from .routers import (
    agents,
    approvals,
    bundles,
    config,
    control,
    deployments,
    evals,
    github,
    observability,
    runs,
    state,
)
from .storage import BundleStore


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    engine = create_engine()
    app.state.engine = engine
    app.state.sessionmaker = create_sessionmaker(engine)
    http_client = httpx.AsyncClient(timeout=10.0)
    app.state.http_client = http_client
    app.state.langfuse = LangfuseClient(settings, http_client)
    store = BundleStore(settings)
    await store.ensure_bucket()
    app.state.bundle_store = store
    # Lazy: resolving the cluster/credentials is deferred to the first pod-log
    # read so an absent/expired credential does not surface as a boot-time ERROR
    # for a proxy most runs never touch.
    app.state.pod_log_reader = build_lazy_pod_log_reader(settings.kube_config_path)
    app.state.pod_lister = build_lazy_pod_lister(settings.kube_config_path)
    valkey: redis.Redis = redis.from_url(settings.valkey_dsn())
    app.state.valkey = valkey
    app.state.kill_switch = KillSwitch(valkey)
    app.state.approval_notifier = ApprovalNotifier(valkey)
    app.state.eval_queue = EvalQueue(valkey)
    app.state.github_reporter = GitHubStatusReporter(
        http_client,
        api_url=settings.github_api_url,
        token=settings.github_token,
        context=settings.eval_check_context,
    )
    try:
        yield
    finally:
        await valkey.aclose()
        await http_client.aclose()
        await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="AgentOS API", version="0.1.0", lifespan=lifespan)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(config.router)
    app.include_router(agents.router)
    app.include_router(approvals.router)
    app.include_router(deployments.router)
    app.include_router(bundles.router)
    app.include_router(github.router)
    app.include_router(observability.router)
    app.include_router(control.router)
    app.include_router(evals.router)
    app.include_router(runs.router)
    app.include_router(state.router)
    return app


app = create_app()
