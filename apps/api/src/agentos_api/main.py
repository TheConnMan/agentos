"""FastAPI application factory and shared-resource lifespan.

The engine, sessionmaker, httpx client, and Langfuse client are created once at
startup and stored on app.state; dependencies (deps.py) read them per request.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as redis
from fastapi import FastAPI

from .config import get_settings
from .db import create_engine, create_sessionmaker
from .k8s import build_pod_log_reader
from .killswitch import KillSwitch
from .langfuse import LangfuseClient
from .routers import (
    agents,
    bundles,
    control,
    deployments,
    github,
    observability,
    runs,
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
    app.state.pod_log_reader = build_pod_log_reader(settings.kube_config_path)
    valkey: redis.Redis = redis.from_url(settings.valkey_dsn())
    app.state.valkey = valkey
    app.state.kill_switch = KillSwitch(valkey)
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

    app.include_router(agents.router)
    app.include_router(deployments.router)
    app.include_router(bundles.router)
    app.include_router(github.router)
    app.include_router(observability.router)
    app.include_router(control.router)
    app.include_router(runs.router)
    return app


app = create_app()
