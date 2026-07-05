"""Shared fixtures: Alembic-migrated compose Postgres + a TestClient.

Integration tests run against the REAL dev-stack Postgres (compose.dev.yaml, port
55434); nothing here mocks the database. The `clean_db` fixture is opt-in so the
pure unit tests (tree reconstruction, mocked proxy) need no database at all.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from agentos_api.config import get_settings
from agentos_api.main import create_app
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

API_DIR = Path(__file__).resolve().parents[1]
ALEMBIC_DIR = API_DIR / "alembic"


@pytest.fixture(scope="session")
def migrated() -> None:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    command.upgrade(cfg, "head")


async def _truncate() -> None:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "TRUNCATE agentos.deployments, agentos.agent_versions, "
                    "agentos.agents CASCADE"
                )
            )
    finally:
        await engine.dispose()


@pytest.fixture
def clean_db(migrated: None) -> None:
    asyncio.run(_truncate())


@pytest.fixture
def client() -> Any:
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": get_settings().api_key}
