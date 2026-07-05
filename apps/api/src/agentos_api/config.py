"""Runtime configuration for the AgentOS API server.

All values default to the compose dev stack (see compose.dev.yaml and
.env.example) so a local run needs no .env. Override any field via the matching
environment variable for shared or production deployments.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Postgres (async driver). Dedicated `agentos` schema keeps our tables clear
    # of Langfuse's own tables on the same database.
    database_url: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:55434/postgres"
    )
    db_schema: str = "agentos"

    # Single shared API key. Dev-only default; override in any shared deployment.
    api_key: str = "agentos-dev-key"

    # Langfuse proxy target (the dev project keys baked into compose.dev.yaml).
    langfuse_host: str = "http://localhost:3001"
    langfuse_public_key: str = "pk-lf-agentos-dev"
    langfuse_secret_key: str = "sk-lf-agentos-dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()
