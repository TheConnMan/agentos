"""FastAPI application factory and shared-resource lifespan.

The engine, sessionmaker, httpx client, and Langfuse client are created once at
startup and stored on app.state; dependencies (deps.py) read them per request.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from .config import get_settings
from .db import create_engine, create_sessionmaker
from .langfuse import LangfuseClient
from .routers import agents, bundles, deployments, runs
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
    try:
        yield
    finally:
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
    app.include_router(runs.router)
    return app


app = create_app()
